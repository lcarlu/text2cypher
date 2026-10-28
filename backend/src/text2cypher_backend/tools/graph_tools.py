"""Core tools of the Text2Cypher agent."""

import json
import logging

from langchain_core.tools import BaseTool, tool

from text2cypher_backend.graph.executor import CypherError, explain_cypher
from text2cypher_backend.tools.context import ToolContext

logger = logging.getLogger(__name__)

# Above this size, get_graph_schema returns an overview and the agent must use search_graph_schema for details
FULL_SCHEMA_MAX_CHARS = 20_000


def create_tools(ctx: ToolContext) -> list[BaseTool]:
    @tool
    def get_graph_schema() -> str:
        """Return the graph schema: node labels with counts and properties, relationship types, patterns
        (with direction), search indexes and modeling notes. Call it before writing your first Cypher query."""
        schema = ctx.schema_cache.get()
        text = schema.to_text()
        if len(text) <= FULL_SCHEMA_MAX_CHARS:
            return text
        overview = ["The schema is too large to be shown entirely. Overview (use search_graph_schema for properties):"]
        overview += [f"- :{label.name} ({label.count or 0:,} nodes)" for label in schema.labels.values()]
        overview += [f"- {p}" for p in schema.patterns]
        return "\n".join(overview)[:FULL_SCHEMA_MAX_CHARS]

    @tool
    def search_graph_schema(question: str) -> str:
        """Return only the part of the graph schema relevant for a question (labels, relationships, properties,
        sample values, business glossary definitions), found by semantic similarity. Prefer it on large graphs.
        Pass the user question, possibly reformulated with business synonyms."""
        from text2cypher_backend.semantic.layer import search_schema

        try:
            text, matches = search_schema(ctx.driver, question, ctx.schema_cache.get())
        except Exception as e:
            logger.exception("search_graph_schema failed")
            return f"Semantic search failed ({e}); call get_graph_schema instead."
        ctx.extra.setdefault("schema_matches", []).append({"question": question, **matches})
        return text if matches["matches"] else "No relevant schema element found; call get_graph_schema."

    @tool
    def get_property_values(label_or_type: str, property: str, contains: str | None = None, limit: int = 25) -> str:
        """Return the most frequent values of a node / relationship property, with their counts (sampled on the
        first 100,000 elements). Use it to check the exact spelling of a value before filtering on it.
        `contains` filters values case-insensitively (e.g. contains='engin' for engineering titles)."""
        schema = ctx.schema_cache.get()
        if label_or_type in schema.labels:
            items, match = schema.labels, f"MATCH (n:`{label_or_type}`)"
        elif label_or_type in schema.relationships:
            items, match = schema.relationships, f"MATCH ()-[n:`{label_or_type}`]->()"
        else:
            return f"Unknown label or relationship type '{label_or_type}'."
        if property not in {p.name for p in items[label_or_type].properties}:
            return f"Unknown property '{property}' on '{label_or_type}'."
        query = (
            f"{match} WITH n LIMIT 100000 WITH n[$property] AS value WHERE value IS NOT NULL"
            + (" AND toLower(toString(value)) CONTAINS toLower($contains)" if contains else "")
            + " RETURN value, count(*) AS count ORDER BY count DESC LIMIT $limit"
        )
        params = {"property": property, "contains": contains, "limit": min(max(limit, 1), 100)}
        try:
            result = ctx.run_cypher(query, params, tool="get_property_values", kind="helper")
        except CypherError as e:
            return f"Error: {e.message}"
        return result.to_llm_text(max_rows=100)

    @tool
    def validate_cypher(query: str) -> str:
        """Check a Cypher query without running it (syntax, unknown labels / properties warnings, read-only).
        Useful for expensive queries on the big graph."""
        try:
            summary = explain_cypher(ctx.driver, query)
        except CypherError as e:
            return f"Invalid query: {e.message}"
        if summary.query_type != "r" and ctx.settings.cypher_read_only:
            return f"Query type '{summary.query_type}' is not allowed: the graph is read-only."
        warnings = [f"{n['title']}: {n['description']}" for n in summary.notifications]
        return "Valid read query." + (" Warnings:\n" + "\n".join(warnings) if warnings else "")

    @tool
    def run_cypher(query: str, parameters: str | None = None) -> str:
        """Execute a read-only Cypher query on the knowledge graph and return the rows.
        Write the full query, using only labels, relationship types and properties of the schema.
        `parameters` is an optional JSON object for $parameters used in the query.
        On error, fix the query and try again."""
        try:
            params = json.loads(parameters) if parameters else None
        except json.JSONDecodeError as e:
            return f"Invalid JSON parameters: {e}"
        try:
            result = ctx.run_cypher(query, params)
        except CypherError as e:
            return f"Cypher error: {e.message}"
        return result.to_llm_text()

    tools = [get_graph_schema, get_property_values, validate_cypher, run_cypher]
    if ctx.semantic_layer:
        tools.insert(1, search_graph_schema)
    return tools
