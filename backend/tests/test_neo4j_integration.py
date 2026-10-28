"""Integration tests against the configured Neo4j (skipped when unreachable). Read-only: they never write."""

import pytest

from text2cypher_backend.graph.driver import get_driver, is_alive
from text2cypher_backend.graph.executor import (
    CypherError,
    CypherSyntaxErrorDetail,
    CypherWriteForbidden,
    execute_cypher,
)
from text2cypher_backend.graph.schema import SchemaCache
from text2cypher_backend.tools import ToolContext, build_tools


@pytest.fixture(scope="module")
def driver():
    driver = get_driver()
    if not is_alive(driver):
        pytest.skip("Neo4j not reachable")
    yield driver
    driver.close()


def test_every_result_type(driver):
    result = execute_cypher(
        driver,
        """
        WITH {name: 'a'} AS props
        RETURN 1 AS int, 1.5 AS float, 'x' AS str, true AS bool, null AS nothing, [1, 'a'] AS list,
               {k: [1, {n: 2}]} AS map, date('2024-01-31') AS date, datetime('2024-01-31T10:00:00Z') AS datetime,
               localdatetime('2024-01-31T10:00:00') AS localdatetime, time('10:00:00+01:00') AS time,
               localtime('10:00:00') AS localtime, duration('P1Y2M3DT4H') AS duration,
               point({longitude: 2.35, latitude: 48.85}) AS geo, point({x: 1, y: 2, z: 3}) AS cartesian,
               0.0 / 0.0 AS nan
        """,
    )
    row = result.rows[0]
    assert row["int"] == 1 and row["list"] == [1, "a"] and row["map"] == {"k": [1, {"n": 2}]}
    assert row["date"] == "2024-01-31" and row["duration"].startswith("P1Y2M3DT4H")
    assert row["geo"]["srid"] == 4326 and row["cartesian"]["z"] == 3.0
    assert row["nan"] == "nan" and row["nothing"] is None
    assert result.summary.query_type == "r"


def test_graph_elements_are_collected(driver):
    result = execute_cypher(driver, "MATCH p = ()-[r]->() RETURN p, r, startNode(r) AS n LIMIT 3")
    if not result.rows:
        pytest.skip("Empty graph")
    row = result.rows[0]
    assert row["p"]["_type"] == "path" and row["r"]["_type"] == "relationship" and row["n"]["_type"] == "node"
    assert result.graph.relationships and result.graph.nodes
    node_ids = {n["elementId"] for n in result.graph.nodes}
    assert all(r["startNodeElementId"] in node_ids for r in result.graph.relationships)


def test_virtual_graph_from_procedure(driver):
    result = execute_cypher(driver, "CALL db.schema.visualization() YIELD nodes, relationships RETURN *")
    assert result.columns == ["nodes", "relationships"]


def test_truncation(driver):
    result = execute_cypher(driver, "UNWIND range(1, 50) AS i RETURN i", max_rows=10)
    assert result.row_count == 10 and result.truncated


def test_writes_are_forbidden(driver):
    with pytest.raises(CypherWriteForbidden):
        execute_cypher(driver, "CREATE (n:ShouldNotExist) RETURN n")
    with pytest.raises(CypherWriteForbidden):
        # Not caught by the static check (procedure): caught by EXPLAIN query type / READ session
        execute_cypher(driver, "CALL apoc.create.node(['ShouldNotExist'], {}) YIELD node RETURN node")


def test_syntax_error(driver):
    with pytest.raises(CypherSyntaxErrorDetail):
        execute_cypher(driver, "MATCH (n RETURN n")
    with pytest.raises(CypherError):
        execute_cypher(driver, "")


def test_tools_registry(driver):
    ctx = ToolContext(driver=driver, schema_cache=SchemaCache(driver), semantic_layer=False)
    tools = {t.name: t for t in build_tools(ctx)}
    assert {"get_graph_schema", "run_cypher", "get_property_values", "validate_cypher"} <= set(tools)
    assert "search_graph_schema" not in tools
    output = tools["run_cypher"].invoke({"query": "RETURN 42 AS answer"})
    assert '"answer": 42' in output
    assert ctx.final_result.query == "RETURN 42 AS answer"
    assert "not allowed" in tools["run_cypher"].invoke({"query": "CREATE (n) RETURN n"})
    assert ctx.traces[-1].status == "error"
