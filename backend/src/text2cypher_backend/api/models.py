from typing import Any, Literal

from pydantic import BaseModel, Field

from text2cypher_backend.graph.executor import CypherResult


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    cypher: str | None = Field(None, description="Cypher used for an assistant answer (helps follow-up questions)")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    history: list[HistoryMessage] = Field(default_factory=list, description="Previous messages, oldest first")


class QuerySummary(BaseModel):
    tool: str
    kind: str
    query: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: str
    error: str | None = None
    row_count: int | None = None
    elapsed_ms: float | None = None


class ChatResponse(BaseModel):
    answer: str
    cypher: str | None = Field(None, description="Query that produced the answer (last successful answer query)")
    result: CypherResult | None = Field(None, description="Full result of `cypher`")
    queries: list[QuerySummary] = Field(default_factory=list, description="Every query run by the tools, in order")
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    schema_matches: list[dict[str, Any]] | None = None
    usage: dict[str, Any] | None = None
    elapsed_ms: float | None = None


class CypherRequest(BaseModel):
    query: str = Field(..., min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    max_rows: int | None = Field(None, ge=1, le=100_000)


class SchemaSearchRequest(BaseModel):
    question: str = Field(..., min_length=1)
