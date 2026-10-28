"""Template for a custom tool: copy this file to tools/custom/<my_tools>.py (without the leading "_").

Guidelines
* The docstring is what the LLM reads to decide when and how to call the tool: be explicit.
* Typed arguments become the tool JSON schema; keep them simple (str, int, float, bool, list[str]).
* Run Cypher through ctx.run_cypher(...): it applies the read-only guard-rails and records the query,
  so the API returns it with its result (kind="answer") or only in the tools trace (kind="helper").
* Return a short string: it goes back into the LLM context (result.to_llm_text() is compact already).
* Return errors as strings rather than raising, so the agent can recover.
"""

from langchain_core.tools import BaseTool, tool

from text2cypher_backend.graph.executor import CypherError
from text2cypher_backend.tools.context import ToolContext


def create_tools(ctx: ToolContext) -> list[BaseTool]:
    @tool
    def count_nodes(label: str) -> str:
        """Return the number of nodes having a given label."""
        if label not in ctx.schema_cache.get().labels:
            return f"Unknown label '{label}'."
        try:
            result = ctx.run_cypher(f"MATCH (n:`{label}`) RETURN count(n) AS count", tool="count_nodes")
        except CypherError as e:
            return f"Error: {e.message}"
        return result.to_llm_text()

    return [count_nodes]
