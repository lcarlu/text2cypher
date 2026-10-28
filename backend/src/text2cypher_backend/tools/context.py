import threading
from dataclasses import dataclass, field
from typing import Any, Literal

import neo4j
from pydantic import BaseModel

from text2cypher_backend.config import Settings, get_settings
from text2cypher_backend.graph.executor import CypherError, CypherResult, execute_cypher
from text2cypher_backend.graph.schema import SchemaCache


class QueryTrace(BaseModel):
    """One Cypher execution made by a tool during an agent run."""

    tool: str
    # "answer" queries fetch data for the user, "helper" ones only help writing Cypher (values lookup...)
    kind: Literal["answer", "helper"] = "answer"
    query: str
    parameters: dict[str, Any] = {}
    status: Literal["success", "error"]
    error: str | None = None
    result: CypherResult | None = None


@dataclass
class ToolContext:
    """Everything a tool needs, created once per chat request (tools must not keep state elsewhere)."""

    driver: neo4j.Driver
    schema_cache: SchemaCache
    semantic_layer: bool = False
    settings: Settings = field(default_factory=get_settings)
    traces: list[QueryTrace] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def run_cypher(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        tool: str = "run_cypher",
        kind: Literal["answer", "helper"] = "answer",
        **kwargs,
    ) -> CypherResult:
        """execute_cypher + trace, so that the API can return every query run by any tool (custom ones included)."""
        try:
            result = execute_cypher(self.driver, query, parameters, **kwargs)
        except CypherError as e:
            self._record(
                QueryTrace(
                    tool=tool,
                    kind=kind,
                    query=e.query or query,
                    parameters=parameters or {},
                    status="error",
                    error=e.message,
                )
            )
            raise
        self._record(
            QueryTrace(
                tool=tool, kind=kind, query=result.query, parameters=parameters or {}, status="success", result=result
            )
        )
        return result

    def _record(self, trace: QueryTrace) -> None:
        with self._lock:
            self.traces.append(trace)

    @property
    def final_result(self) -> QueryTrace | None:
        """Last successful "answer" query: the one that answered the question."""
        for trace in reversed(self.traces):
            if trace.kind == "answer" and trace.status == "success":
                return trace
        return None
