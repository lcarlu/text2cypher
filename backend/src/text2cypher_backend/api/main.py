"""Text2Cypher HTTP API.

uv run text2cypher-api                       # http://127.0.0.1:8000, docs on /docs
uv run text2cypher-api --port 8001 --reload
"""

import argparse
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from text2cypher_backend.agent.agent import run_agent
from text2cypher_backend.api.models import (
    ChatRequest,
    ChatResponse,
    CypherRequest,
    QuerySummary,
    SchemaSearchRequest,
)
from text2cypher_backend.config import get_settings
from text2cypher_backend.graph.driver import get_driver, is_alive
from text2cypher_backend.graph.executor import (
    CypherError,
    CypherResult,
    CypherSummary,
    CypherWriteForbidden,
    execute_cypher,
    explain_cypher,
)
from text2cypher_backend.graph.schema import SchemaCache
from text2cypher_backend.semantic.layer import search_schema, semantic_layer_exists
from text2cypher_backend.tools import ToolContext, build_tools

logger = logging.getLogger("text2cypher.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    driver = get_driver()
    app.state.driver = driver
    app.state.schema_cache = SchemaCache(driver)
    try:
        driver.verify_connectivity()
        app.state.semantic_layer = await run_in_threadpool(semantic_layer_exists, driver)
        await run_in_threadpool(app.state.schema_cache.get)
        logger.info("Connected to %s (semantic layer: %s)", get_settings().neo4j_uri, app.state.semantic_layer)
    except Exception as e:
        # Start anyway: /health reports the problem and the schema is loaded on first use
        app.state.semantic_layer = False
        logger.error("Neo4j not reachable at startup: %s", e)
    yield
    driver.close()


app = FastAPI(
    title="Text2Cypher API", description="Ask natural language questions to a Neo4j knowledge graph", lifespan=lifespan
)


@app.exception_handler(CypherError)
async def cypher_error_handler(request: Request, exc: CypherError):
    status = 403 if isinstance(exc, CypherWriteForbidden) else 400
    return JSONResponse(status_code=status, content={"detail": exc.message, "code": exc.code, "query": exc.query})


def _tool_context() -> ToolContext:
    return ToolContext(
        driver=app.state.driver, schema_cache=app.state.schema_cache, semantic_layer=app.state.semantic_layer
    )


@app.get("/health")
async def health():
    alive = await run_in_threadpool(is_alive, app.state.driver)
    return {"status": "ok" if alive else "degraded", "neo4j": alive, "semantic_layer": app.state.semantic_layer}


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest):
    """Answer a natural language question: returns the answer, the Cypher query and its full result."""
    ctx = _tool_context()
    start = time.perf_counter()
    history = [m.model_dump() for m in body.history]
    try:
        run = await run_in_threadpool(run_agent, ctx, body.message, history)
    except Exception as e:
        logger.exception("Agent failed")
        raise HTTPException(status_code=500, detail=f"The agent could not complete this request: {e}") from None
    final = ctx.final_result
    return ChatResponse(
        answer=run.answer,
        cypher=final.query if final else None,
        result=final.result if final else None,
        queries=[
            QuerySummary(
                tool=t.tool,
                kind=t.kind,
                query=t.query,
                parameters=t.parameters,
                status=t.status,
                error=t.error,
                row_count=t.result.row_count if t.result else None,
                elapsed_ms=t.result.summary.elapsed_ms if t.result else None,
            )
            for t in ctx.traces
        ],
        tool_calls=run.tool_calls,
        schema_matches=ctx.extra.get("schema_matches"),
        usage=run.usage,
        elapsed_ms=round((time.perf_counter() - start) * 1000, 1),
    )


@app.post("/cypher", response_model=CypherResult)
async def cypher(body: CypherRequest):
    """Run a Cypher query directly (same guard-rails as the agent)."""
    return await run_in_threadpool(
        lambda: execute_cypher(app.state.driver, body.query, body.parameters, max_rows=body.max_rows)
    )


@app.post("/cypher/explain", response_model=CypherSummary)
async def cypher_explain(body: CypherRequest):
    return await run_in_threadpool(explain_cypher, app.state.driver, body.query, body.parameters)


@app.get("/schema")
async def schema():
    graph_schema = await run_in_threadpool(app.state.schema_cache.get)
    return {"text": graph_schema.to_text(), "schema": graph_schema.model_dump()}


@app.post("/schema/refresh")
async def schema_refresh():
    """Re-introspect the graph (after a data model change or a semantic layer rebuild)."""
    graph_schema = await run_in_threadpool(app.state.schema_cache.refresh)
    app.state.semantic_layer = await run_in_threadpool(semantic_layer_exists, app.state.driver)
    return {
        "labels": len(graph_schema.labels),
        "relationships": len(graph_schema.relationships),
        "semantic_layer": app.state.semantic_layer,
    }


@app.get("/schema/graph")
async def schema_graph():
    """Data model as a graph (one node per label, one relationship per pattern) for visualization."""
    graph_schema = await run_in_threadpool(app.state.schema_cache.get)
    nodes = [
        {
            "_type": "node",
            "elementId": f"label:{label.name}",
            "labels": [label.name],
            "properties": {
                "name": label.name,
                "count": label.count,
                "description": label.description,
                "properties": [p.name for p in label.properties],
            },
        }
        for label in graph_schema.labels.values()
    ]
    relationships = [
        {
            "_type": "relationship",
            "elementId": f"rel:{i}",
            "type": p.type,
            "startNodeElementId": f"label:{p.start}",
            "endNodeElementId": f"label:{p.end}",
            "properties": {
                "description": graph_schema.relationships[p.type].description
                if p.type in graph_schema.relationships
                else None
            },
        }
        for i, p in enumerate(graph_schema.patterns)
    ]
    return {"nodes": nodes, "relationships": relationships}


@app.post("/schema/search")
async def schema_search(body: SchemaSearchRequest):
    """Debug the semantic layer: what search_graph_schema returns for a question."""
    if not app.state.semantic_layer:
        raise HTTPException(status_code=404, detail="No semantic layer: run `uv run text2cypher-semantic`.")
    graph_schema = await run_in_threadpool(app.state.schema_cache.get)
    text, matches = await run_in_threadpool(search_schema, app.state.driver, body.question, graph_schema)
    return {"text": text, **matches}


@app.get("/tools")
async def tools():
    return [{"name": t.name, "description": t.description, "args": t.args} for t in build_tools(_tool_context())]


def run() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Start the Text2Cypher API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run("text2cypher_backend.api.main:app", host=args.host, port=args.port, reload=args.reload)
