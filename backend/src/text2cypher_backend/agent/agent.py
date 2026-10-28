import logging
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from text2cypher_backend.llm import get_llm
from text2cypher_backend.tools import ToolContext, build_tools

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """# Identity
You are a Text2Cypher agent answering business questions on a Neo4j knowledge graph.
You translate the question into Cypher, run it with the tools and answer from the returned data only.

# Workflow
1. Discover the schema before writing Cypher: {schema_hint}
2. Before filtering on a string value (name, title, status, category...), check its exact spelling with
   get_property_values unless the schema already lists the allowed values.
3. Write ONE Cypher query answering the whole question when possible and execute it with run_cypher
   (or a dedicated domain tool when one fits exactly). Never answer data questions without executing a query.
4. On error or empty result, check labels, relationship directions, property names and values, then retry
   (at most 3 attempts). If you still cannot answer, say so.

# Cypher rules
* Use only labels, relationship types, properties and directions present in the schema.
* The graph is read-only: never use CREATE, MERGE, SET, DELETE, REMOVE, DROP, LOAD CSV.
* The graph is very large: aggregate in Cypher (count, avg, collect...) instead of returning raw nodes,
  return only the properties you need, add LIMIT (<= 100) to listings unless the user asks for everything,
  and start traversals from the most selective node.
* Use explicit column aliases with readable names (AS employee_count) and ORDER BY for rankings.
* Return graph elements (nodes / relationships / paths) only when the user asks to see or explore the graph.

# Answer
* Answer in the language of the user question, in concise markdown, from the query results only.
* Do not include the Cypher query in the answer: it is shown to the user separately.
* If the result was truncated, say so.
"""

SCHEMA_HINT_SEMANTIC = (
    "call search_graph_schema with the question (the graph has a semantic layer); "
    "call get_graph_schema if you need the full picture."
)
SCHEMA_HINT_FULL = "call get_graph_schema."


@dataclass
class AgentRun:
    answer: str
    messages: list[BaseMessage]
    usage: dict[str, Any] | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _history_messages(history: list[dict[str, Any]] | None, limit: int) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for item in (history or [])[-limit:]:
        content = item.get("content") or ""
        if item.get("role") == "user":
            messages.append(HumanMessage(content=content))
        else:
            # The previous Cypher helps follow-up questions ("and for women?")
            if item.get("cypher"):
                content += f"\n\n(Cypher used for this answer: {item['cypher']})"
            messages.append(AIMessage(content=content))
    return messages


def _message_text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    blocks = [
        {"text": b} if isinstance(b, str) else b
        for b in message.content
        if isinstance(b, str) or (isinstance(b, dict) and b.get("type") in ("text", "output_text"))
    ]
    # Reasoning models (Responses API) tag interim "commentary" text and the "final_answer"
    final = [b for b in blocks if b.get("phase") == "final_answer"]
    return "".join(b.get("text", "") for b in (final or blocks))


def _serialize_usage(callback: UsageMetadataCallbackHandler) -> dict[str, Any] | None:
    if not callback.usage_metadata:
        return None
    usage: dict[str, Any] = {
        "models": list(callback.usage_metadata),
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    for data in callback.usage_metadata.values():
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            usage[key] += data.get(key, 0)
    return usage


def _tool_calls(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    outputs = {m.tool_call_id: _message_text(m) for m in messages if isinstance(m, ToolMessage)}
    calls = []
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                output = outputs.get(call["id"], "")
                calls.append({"name": call["name"], "args": call["args"], "output": output[:2000]})
    return calls


def run_agent(ctx: ToolContext, question: str, history: list[dict[str, Any]] | None = None) -> AgentRun:
    settings = ctx.settings
    callback = UsageMetadataCallbackHandler()
    prompt = SYSTEM_PROMPT.format(schema_hint=SCHEMA_HINT_SEMANTIC if ctx.semantic_layer else SCHEMA_HINT_FULL)
    agent = create_agent(get_llm(), build_tools(ctx), system_prompt=prompt)
    messages = _history_messages(history, settings.agent_history_messages) + [HumanMessage(content=question.strip())]
    state = agent.invoke(
        {"messages": messages},
        config={"recursion_limit": settings.agent_recursion_limit, "callbacks": [callback]},
    )
    new_messages = state["messages"][len(messages) :]
    final = next((m for m in reversed(new_messages) if isinstance(m, AIMessage) and _message_text(m)), None)
    return AgentRun(
        answer=_message_text(final) if final else "",
        messages=new_messages,
        usage=_serialize_usage(callback),
        tool_calls=_tool_calls(new_messages),
    )
