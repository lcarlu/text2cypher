import pytest

from text2cypher_backend.graph.guard import clean_query, find_write_clause


@pytest.mark.parametrize(
    "query, clause",
    [
        ("CREATE (n:Person) RETURN n", "CREATE"),
        ("MATCH (n) DETACH DELETE n", "DETACH"),
        ("MATCH (n) SET n.x = 1", "SET"),
        ("merge (n:Person {id: 1})", "MERGE"),
        ("MATCH (n) REMOVE n:Label", "REMOVE"),
        ("LOAD CSV FROM 'file:///x.csv' AS row RETURN row", "LOAD CSV"),
        ("MATCH (n) CALL (n) { DELETE n } IN TRANSACTIONS", "DELETE"),
        ("DROP INDEX my_index", "DROP"),
        ("MATCH (n) FOREACH (x IN [1] | SET n.a = x)", "FOREACH"),
    ],
)
def test_write_clauses_are_detected(query, clause):
    assert find_write_clause(query) == clause


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Employee) RETURN count(n)",
        "MATCH (n:Employee) WHERE n.name = 'CREATE' RETURN n",
        'MATCH (n) WHERE n.comment CONTAINS "delete me" RETURN n',
        "MATCH (n:`SET`) RETURN n",
        "MATCH (n) RETURN n.set, n.create AS created",
        "CALL apoc.create.vNode(['X'], {}) YIELD node RETURN node",
        "// CREATE something\nMATCH (n) RETURN n",
        "/* MERGE */ MATCH (n) RETURN n",
        "MATCH (n) WHERE n.x = $set RETURN n",
        "MATCH (d:Dataset) RETURN d.offset, d.createdAt",
    ],
)
def test_read_queries_are_allowed(query):
    assert find_write_clause(query) is None


def test_clean_query_strips_fences_and_semicolons():
    assert clean_query("```cypher\nMATCH (n) RETURN n;\n```") == "MATCH (n) RETURN n"
    assert clean_query("  MATCH (n) RETURN n ;  ") == "MATCH (n) RETURN n"
