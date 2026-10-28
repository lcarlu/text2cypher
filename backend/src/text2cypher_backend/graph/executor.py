"""execute_cypher: the single entry point used by the API and by every agent tool to run Cypher."""

import json
import re
import time
from typing import Any

import neo4j
from neo4j.exceptions import ClientError, CypherSyntaxError, Neo4jError
from pydantic import BaseModel, Field

from text2cypher_backend.config import get_settings
from text2cypher_backend.graph.guard import clean_query, find_write_clause
from text2cypher_backend.graph.serialization import GraphCollector, compact_for_llm, serialize_value

# Query types reported by EXPLAIN: r=read, rw=read/write, w=write, s=schema write
READ_QUERY_TYPES = {"r"}
_EXPLAIN_PREFIX = re.compile(r"^\s*(EXPLAIN|PROFILE)\b", re.IGNORECASE)


class CypherError(Exception):
    def __init__(self, message: str, code: str | None = None, query: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.query = query


class CypherSyntaxErrorDetail(CypherError):
    pass


class CypherWriteForbidden(CypherError):
    pass


class CypherSummary(BaseModel):
    database: str | None = None
    query_type: str | None = None
    counters: dict[str, int] = Field(default_factory=dict)
    result_available_after_ms: int | None = None
    result_consumed_after_ms: int | None = None
    elapsed_ms: float | None = None
    notifications: list[dict[str, Any]] = Field(default_factory=list)


class GraphData(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)


class CypherResult(BaseModel):
    query: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    graph: GraphData = Field(default_factory=GraphData)
    summary: CypherSummary = Field(default_factory=CypherSummary)

    def to_llm_text(self, max_rows: int | None = None) -> str:
        """Compact JSON-lines representation sent back to the LLM by the run_cypher tool."""
        max_rows = max_rows or get_settings().cypher_max_rows_for_llm
        if not self.columns:
            return "Query executed; no columns returned."
        if not self.rows:
            return f"0 rows. Columns: {', '.join(self.columns)}"
        lines = [f"Columns: {', '.join(self.columns)}"]
        for row in self.rows[:max_rows]:
            lines.append(json.dumps(compact_for_llm(row), ensure_ascii=False, default=str))
        hidden = self.row_count - max_rows
        if hidden > 0:
            lines.append(f"... {hidden} more rows not shown (the user sees all {self.row_count} rows).")
        if self.truncated:
            lines.append(f"Result truncated at {self.row_count} rows: aggregate or add LIMIT if you need everything.")
        return "\n".join(lines)


def _summary_to_model(summary: neo4j.ResultSummary, elapsed_ms: float) -> CypherSummary:
    counters = {k: v for k, v in vars(summary.counters).items() if isinstance(v, int) and not isinstance(v, bool) and v}
    notifications = [
        {
            "severity": str(status.raw_severity or ""),
            "classification": str(status.raw_classification or ""),
            "title": status.gql_status,
            "description": status.status_description,
        }
        for status in summary.gql_status_objects
        if status.is_notification
    ]
    return CypherSummary(
        database=summary.database,
        query_type=summary.query_type,
        counters=counters,
        result_available_after_ms=summary.result_available_after,
        result_consumed_after_ms=summary.result_consumed_after,
        elapsed_ms=round(elapsed_ms, 1),
        notifications=notifications,
    )


def _wrap_neo4j_error(error: Neo4jError, query: str) -> CypherError:
    message = error.message or str(error)
    if isinstance(error, CypherSyntaxError):
        return CypherSyntaxErrorDetail(message, error.code, query)
    if (error.code and "AccessMode" in error.code) or "not allowed in read access mode" in message.lower():
        return CypherWriteForbidden(message, error.code, query)
    return CypherError(message, error.code, query)


def explain_cypher(
    driver: neo4j.Driver,
    query: str,
    parameters: dict[str, Any] | None = None,
    *,
    database: str | None = None,
) -> CypherSummary:
    """Plan the query without running it: validates syntax/labels and reports the query type."""
    settings = get_settings()
    query = clean_query(query)
    explain = query if _EXPLAIN_PREFIX.match(query) else f"EXPLAIN {query}"
    start = time.perf_counter()
    try:
        with driver.session(
            database=database or settings.neo4j_database, default_access_mode=neo4j.READ_ACCESS
        ) as session:
            summary = session.run(explain, parameters or {}).consume()
    except Neo4jError as e:
        raise _wrap_neo4j_error(e, query) from e
    return _summary_to_model(summary, (time.perf_counter() - start) * 1000)


def execute_cypher(
    driver: neo4j.Driver,
    query: str,
    parameters: dict[str, Any] | None = None,
    *,
    database: str | None = None,
    read_only: bool | None = None,
    max_rows: int | None = None,
    timeout: float | None = None,
) -> CypherResult:
    """Run a Cypher query and return every kind of result in a JSON-safe structure.

    * rows: one dict per record, values serialized (nodes, relationships, paths, temporal, spatial, vectors, maps, lists...)
    * graph: deduplicated nodes / relationships found anywhere in the result, ready for visualization
    * summary: query type, update counters, timings and server notifications
    Read-only mode (default from settings) rejects writes three times: static check, EXPLAIN query type, READ session.
    Raises CypherError (or a subclass) on invalid / forbidden / failing queries.
    """
    settings = get_settings()
    read_only = settings.cypher_read_only if read_only is None else read_only
    max_rows = max_rows or settings.cypher_max_rows
    timeout = timeout or settings.cypher_timeout_seconds
    database = database or settings.neo4j_database
    parameters = parameters or {}
    query = clean_query(query)
    if not query:
        raise CypherError("Empty query.")

    if read_only:
        clause = find_write_clause(query)
        if clause:
            raise CypherWriteForbidden(f"Write clause '{clause}' is not allowed: the graph is read-only.", query=query)
        plan = explain_cypher(driver, query, parameters, database=database)
        if plan.query_type not in READ_QUERY_TYPES:
            raise CypherWriteForbidden(
                f"Query type '{plan.query_type}' is not allowed: the graph is read-only.", query=query
            )

    access_mode = neo4j.READ_ACCESS if read_only else neo4j.WRITE_ACCESS
    collector = GraphCollector()
    rows: list[dict[str, Any]] = []
    truncated = False
    start = time.perf_counter()
    try:
        with driver.session(database=database, default_access_mode=access_mode) as session:
            result = session.run(neo4j.Query(query, timeout=timeout), parameters)
            columns = list(result.keys())
            for record in result:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append({key: serialize_value(value) for key, value in record.items()})
                for value in record.values():
                    collector.collect(value)
            summary = result.consume()
    except (ClientError, Neo4jError) as e:
        raise _wrap_neo4j_error(e, query) from e

    return CypherResult(
        query=query,
        parameters=serialize_value(parameters),
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        graph=GraphData(**collector.to_dict()),
        summary=_summary_to_model(summary, (time.perf_counter() - start) * 1000),
    )
