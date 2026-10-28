"""Semantic layer over the graph schema, stored in the same database.

    (:SchemaElement {kind: 'label'|'relationship', name, description, text, embedding})
    (:SchemaElement:label)-[:FROM_LABEL|TO_LABEL]-(:SchemaElement:relationship)   patterns
    (:GlossaryTerm {name, definition, embedding})-[:DESCRIBES]->(:SchemaElement)
    (:GlossaryTerm)-[:HAS_TERM]->(:GlossaryTerm)                                   taxonomy

With hundreds of labels the full schema does not fit (or dilutes) the prompt: search_schema embeds the
question and returns only the matching labels / relationships, their neighbours and glossary definitions.
Build it on any graph (your real KG included) with: uv run text2cypher-semantic
"""

import argparse
import json
import logging
import re
import time

import neo4j
import yaml

from text2cypher_backend.config import get_settings
from text2cypher_backend.graph.driver import get_driver
from text2cypher_backend.graph.schema import GraphSchema, PropertySchema, introspect_schema
from text2cypher_backend.llm import get_embeddings

logger = logging.getLogger(__name__)

SCHEMA_INDEX = "schema_element_embedding"
GLOSSARY_INDEX = "glossary_term_embedding"
# STRING properties with at most this many distinct values in the sample are stored as categorical values
MAX_CATEGORICAL_VALUES = 25
VALUE_SAMPLE_SIZE = 10_000
MAX_CATEGORICAL_LENGTH = 60
# Identifiers / contact data are never categorical
IDENTIFIER_PROPERTY = re.compile(r"(^id$|Id$|_id$|email|phone|url|uri|password|token)", re.IGNORECASE)

SAMPLE_NODE_VALUES = """
MATCH (n:`{label}`) WITH n LIMIT $sample
WITH n[$property] AS value WHERE value IS NOT NULL
RETURN collect(DISTINCT value)[..$max] AS values
"""
SAMPLE_REL_VALUES = """
MATCH ()-[r:`{type}`]->() WITH r LIMIT $sample
WITH r[$property] AS value WHERE value IS NOT NULL
RETURN collect(DISTINCT value)[..$max] AS values
"""
_SEARCH_RETURN = """
RETURN element.kind AS kind, element.name AS name, max(score) AS score,
       [t IN collect(DISTINCT term) WHERE t IS NOT NULL | {name: t.name, definition: t.definition}] AS terms
ORDER BY score DESC
"""
# Neo4j 2025.x+: Cypher 25 SEARCH clause
SEARCH_SCHEMA = f"""CYPHER 25
CALL () {{
    MATCH (element:SchemaElement)
        SEARCH element IN (VECTOR INDEX {SCHEMA_INDEX} FOR $embedding LIMIT $k) SCORE AS score
    WITH element, score WHERE score >= $min_score
    RETURN element, score, null AS term
    UNION
    MATCH (term:GlossaryTerm)
        SEARCH term IN (VECTOR INDEX {GLOSSARY_INDEX} FOR $embedding LIMIT $k) SCORE AS score
    WITH term, score WHERE score >= $min_score
    MATCH (term)-[:HAS_TERM*0..3]->(:GlossaryTerm)-[:DESCRIBES]->(element:SchemaElement)
    RETURN element, score, term
}}
{_SEARCH_RETURN}"""
# Neo4j 5.x fallback (plain CALL { } subquery: the scoped "CALL () { }" syntax needs 5.23+)
SEARCH_SCHEMA_LEGACY = f"""
CALL {{
    CALL db.index.vector.queryNodes('{SCHEMA_INDEX}', $k, $embedding) YIELD node, score
    WITH node, score WHERE score >= $min_score
    RETURN node AS element, score, null AS term
    UNION
    CALL db.index.vector.queryNodes('{GLOSSARY_INDEX}', $k, $embedding) YIELD node, score
    WITH node, score WHERE score >= $min_score
    MATCH (node)-[:HAS_TERM*0..3]->(:GlossaryTerm)-[:DESCRIBES]->(element:SchemaElement)
    RETURN element, score, node AS term
}}
{_SEARCH_RETURN}"""


def _sample_values(session: neo4j.Session, query: str, prop: PropertySchema) -> list | None:
    if prop.type != "STRING" or prop.values or IDENTIFIER_PROPERTY.search(prop.name):
        return prop.values
    record = session.run(query, sample=VALUE_SAMPLE_SIZE, property=prop.name, max=MAX_CATEGORICAL_VALUES + 1).single()
    values = record["values"] if record else []
    if not 0 < len(values) <= MAX_CATEGORICAL_VALUES or any(len(v) > MAX_CATEGORICAL_LENGTH for v in values):
        return None
    return sorted(values)


def _element_text(
    kind: str, name: str, description: str | None, properties: list[PropertySchema], extra: str = ""
) -> str:
    """Text embedded for similarity search: name, business description, properties and their descriptions."""
    props = "; ".join(f"{p.name}" + (f" ({p.description})" if p.description else "") for p in properties)
    return f"{kind} {name}: {description or ''} {extra} Properties: {props}".strip()


def _flatten_glossary(terms: list[dict], parent: str | None = None) -> list[dict]:
    flat = []
    for term in terms:
        flat.append(
            {
                "name": term["term"],
                "definition": term.get("definition", ""),
                "parent": parent,
                "labels": term.get("labels", []),
                "relationships": term.get("relationships", []),
            }
        )
        flat.extend(_flatten_glossary(term.get("terms", []), term["term"]))
    return flat


def build_semantic_layer(driver: neo4j.Driver, *, database: str | None = None) -> GraphSchema:
    settings = get_settings()
    database = database or settings.neo4j_database
    start = time.perf_counter()
    logger.info("Introspecting graph schema...")
    schema = introspect_schema(driver, database=database, with_stored_values=False)
    embeddings = get_embeddings()

    with driver.session(database=database) as session:
        logger.info("Sampling categorical values...")
        for label in schema.labels.values():
            for prop in label.properties:
                prop.values = _sample_values(session, SAMPLE_NODE_VALUES.format(label=label.name), prop)
        for rel in schema.relationships.values():
            for prop in rel.properties:
                prop.values = _sample_values(session, SAMPLE_REL_VALUES.format(type=rel.name), prop)

        elements = []
        for label in schema.labels.values():
            neighbours = sorted({p.type for p in schema.patterns_for({label.name})})
            elements.append(
                {
                    "kind": "label",
                    "name": label.name,
                    "description": label.description,
                    "count": label.count,
                    "properties": json.dumps([p.model_dump(exclude_none=True) for p in label.properties]),
                    "text": _element_text(
                        "Node label",
                        label.name,
                        label.description,
                        label.properties,
                        f"Relationships: {', '.join(neighbours)}.",
                    ),
                }
            )
        for rel in schema.relationships.values():
            patterns = [str(p) for p in schema.patterns if p.type == rel.name]
            elements.append(
                {
                    "kind": "relationship",
                    "name": rel.name,
                    "description": rel.description,
                    "count": None,
                    "properties": json.dumps([p.model_dump(exclude_none=True) for p in rel.properties]),
                    "text": _element_text(
                        "Relationship", rel.name, rel.description, rel.properties, f"Patterns: {', '.join(patterns)}."
                    ),
                }
            )
        glossary_path = settings.glossary_path
        terms = []
        if glossary_path.exists():
            with open(glossary_path, encoding="utf-8") as f:
                terms = _flatten_glossary(yaml.safe_load(f) or [])

        logger.info("Embedding %d schema elements and %d glossary terms...", len(elements), len(terms))
        vectors = embeddings.embed_documents(
            [e["text"] for e in elements] + [f"{t['name']}: {t['definition']}" for t in terms]
        )
        for element, vector in zip(elements, vectors[: len(elements)], strict=True):
            element["embedding"] = vector
        for term, vector in zip(terms, vectors[len(elements) :], strict=True):
            term["embedding"] = vector
        dimensions = len(vectors[0])

        session.run("MATCH (n:SchemaElement|GlossaryTerm) DETACH DELETE n").consume()
        for index in (SCHEMA_INDEX, GLOSSARY_INDEX):
            session.run(f"DROP INDEX {index} IF EXISTS").consume()
        session.run(
            """
            UNWIND $elements AS e
            CREATE (s:SchemaElement)
            SET s = e {.kind, .name, .description, .count, .properties, .text}
            WITH s, e CALL db.create.setNodeVectorProperty(s, 'embedding', e.embedding)
            """,
            elements=elements,
        ).consume()
        session.run(
            """
            UNWIND $patterns AS p
            MATCH (start:SchemaElement {kind: 'label', name: p.start}), (end:SchemaElement {kind: 'label', name: p.end}),
                  (rel:SchemaElement {kind: 'relationship', name: p.type})
            MERGE (start)<-[:FROM_LABEL]-(rel)
            MERGE (rel)-[:TO_LABEL]->(end)
            """,
            patterns=[p.model_dump() for p in schema.patterns],
        ).consume()
        session.run(
            """
            UNWIND $terms AS t
            CREATE (g:GlossaryTerm {name: t.name, definition: t.definition})
            WITH g, t CALL db.create.setNodeVectorProperty(g, 'embedding', t.embedding)
            WITH g, t
            UNWIND t.labels + t.relationships AS target
            MATCH (s:SchemaElement {name: target})
            MERGE (g)-[:DESCRIBES]->(s)
            """,
            terms=terms,
        ).consume()
        session.run(
            """
            UNWIND $terms AS t
            WITH t WHERE t.parent IS NOT NULL
            MATCH (parent:GlossaryTerm {name: t.parent}), (child:GlossaryTerm {name: t.name})
            MERGE (parent)-[:HAS_TERM]->(child)
            """,
            terms=terms,
        ).consume()
        for index, label in ((SCHEMA_INDEX, "SchemaElement"), (GLOSSARY_INDEX, "GlossaryTerm")):
            session.run(
                f"""CREATE VECTOR INDEX {index} IF NOT EXISTS FOR (n:{label}) ON (n.embedding)
                OPTIONS {{indexConfig: {{`vector.dimensions`: $dimensions, `vector.similarity_function`: 'cosine'}}}}""",
                dimensions=dimensions,
            ).consume()
        session.run("CALL db.awaitIndexes(300)").consume()

    logger.info("Semantic layer built in %.1fs", time.perf_counter() - start)
    return schema


def semantic_layer_exists(driver: neo4j.Driver, database: str | None = None) -> bool:
    with driver.session(
        database=database or get_settings().neo4j_database, default_access_mode=neo4j.READ_ACCESS
    ) as session:
        record = session.run(
            "SHOW INDEXES YIELD name, state WHERE name = $name AND state = 'ONLINE' RETURN count(*) AS c",
            name=SCHEMA_INDEX,
        ).single()
        return bool(record and record["c"])


def search_schema(
    driver: neo4j.Driver,
    question: str,
    schema: GraphSchema,
    *,
    database: str | None = None,
) -> tuple[str, dict]:
    """Return (schema text restricted to the elements relevant for the question, matches for tracing)."""
    settings = get_settings()
    params = {
        "embedding": get_embeddings().embed_query(question),
        "k": settings.semantic_top_k,
        "min_score": settings.semantic_min_score,
    }
    with driver.session(database=database or settings.neo4j_database, default_access_mode=neo4j.READ_ACCESS) as session:
        try:
            rows = session.run(SEARCH_SCHEMA, params).data()
        except neo4j.exceptions.CypherSyntaxError:
            rows = session.run(SEARCH_SCHEMA_LEGACY, params).data()
    labels = {r["name"] for r in rows if r["kind"] == "label"}
    rel_types = {r["name"] for r in rows if r["kind"] == "relationship"}
    # Endpoints of matched relationships are needed to write the pattern
    labels |= {p.start for p in schema.patterns if p.type in rel_types} | {
        p.end for p in schema.patterns if p.type in rel_types
    }
    # One hop around matched labels so that joins through intermediate nodes are visible
    labels |= {p.end for p in schema.patterns if p.start in labels} | {
        p.start for p in schema.patterns if p.end in labels
    }
    terms = {t["name"]: t["definition"] for r in rows for t in r["terms"]}

    text = schema.to_text(labels=labels, rel_types=rel_types)
    if terms:
        text = "# Business glossary\n" + "\n".join(f"- {n}: {d}" for n, d in terms.items()) + "\n" + text
    return text, {"matches": [{k_: r[k_] for k_ in ("kind", "name", "score")} for r in rows], "terms": list(terms)}


def main() -> None:
    parser = argparse.ArgumentParser(description="(Re)build the semantic layer of the configured Neo4j database")
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    driver = get_driver()
    try:
        build_semantic_layer(driver)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
