"""Graph schema introspection (labels, relationship types, properties, patterns, search indexes)
merged with the business descriptions of schema_descriptions.yaml."""

import json
import logging
import threading
from pathlib import Path
from typing import Any

import neo4j
import yaml
from pydantic import BaseModel, Field

from text2cypher_backend.config import get_settings

logger = logging.getLogger(__name__)

APOC_NODE_PROPERTIES = """
CALL apoc.meta.data({sample: $sample, maxRels: $sample})
YIELD label, other, elementType, type, property
WHERE NOT type = "RELATIONSHIP" AND elementType = "node" AND NOT label IN $excluded
RETURN label, collect({name: property, type: type}) AS properties
"""
APOC_PATTERNS = """
CALL apoc.meta.data({sample: $sample, maxRels: $sample})
YIELD label, other, elementType, type, property
WHERE type = "RELATIONSHIP" AND elementType = "node" AND NOT label IN $excluded
UNWIND other AS other_node
WITH label, property, other_node WHERE NOT other_node IN $excluded
RETURN DISTINCT label AS start, property AS type, other_node AS end
"""
# Fallbacks without APOC (slower on big graphs: they scan the stores)
NATIVE_NODE_PROPERTIES = """
CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName, propertyTypes
UNWIND nodeLabels AS label
WITH label, propertyName, propertyTypes WHERE NOT label IN $excluded
RETURN label, collect(CASE WHEN propertyName IS NULL THEN null ELSE {name: propertyName, type: propertyTypes[0]} END) AS properties
"""
NATIVE_PATTERNS = """
CALL db.schema.visualization() YIELD relationships
UNWIND relationships AS rel
WITH labels(startNode(rel))[0] AS start, type(rel) AS type, labels(endNode(rel))[0] AS end
WHERE NOT start IN $excluded AND NOT end IN $excluded
RETURN DISTINCT start, type, end
"""
# Per relationship type sampling (uses the relationship type lookup index): label-based sampling misses rare
# relationship types on big graphs (e.g. 24 MANAGES among 300k employees)
REL_TYPE_PATTERNS = """
MATCH (a)-[r:`{type}`]->(b) WITH labels(a) AS starts, labels(b) AS ends LIMIT $sample
UNWIND starts AS start UNWIND ends AS end
WITH DISTINCT start, end WHERE NOT start IN $excluded AND NOT end IN $excluded
RETURN start, end
"""
REL_TYPE_PROPERTIES = """
MATCH ()-[r:`{type}`]->() WITH r LIMIT $sample
UNWIND keys(r) AS name
WITH name, replace(valueType(r[name]), ' NOT NULL', '') AS type
RETURN name, collect(DISTINCT type)[0] AS type
"""
SEARCH_INDEXES = """
SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, state
WHERE type IN ['FULLTEXT', 'VECTOR'] AND state = 'ONLINE'
RETURN name, type, entityType, labelsOrTypes, properties
"""


class PropertySchema(BaseModel):
    name: str
    type: str | None = None
    description: str | None = None
    values: list[Any] | None = None


class LabelSchema(BaseModel):
    name: str
    count: int | None = None
    description: str | None = None
    properties: list[PropertySchema] = Field(default_factory=list)


class RelationshipSchema(BaseModel):
    name: str
    description: str | None = None
    properties: list[PropertySchema] = Field(default_factory=list)


class Pattern(BaseModel):
    start: str
    type: str
    end: str

    def __str__(self) -> str:
        return f"(:{self.start})-[:{self.type}]->(:{self.end})"


class SearchIndex(BaseModel):
    name: str
    type: str
    entity_type: str
    labels_or_types: list[str]
    properties: list[str]


class GraphSchema(BaseModel):
    labels: dict[str, LabelSchema] = Field(default_factory=dict)
    relationships: dict[str, RelationshipSchema] = Field(default_factory=dict)
    patterns: list[Pattern] = Field(default_factory=list)
    indexes: list[SearchIndex] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def patterns_for(self, labels: set[str] | None = None, rel_types: set[str] | None = None) -> list[Pattern]:
        labels, rel_types = labels or set(), rel_types or set()
        return [p for p in self.patterns if p.start in labels or p.end in labels or p.type in rel_types]

    def to_text(self, labels: set[str] | None = None, rel_types: set[str] | None = None) -> str:
        """Compact schema description for the LLM. Restrict to some labels / relationship types when given."""
        if labels is None and rel_types is None:
            label_items = list(self.labels.values())
            rel_items = list(self.relationships.values())
            patterns = self.patterns
        else:
            patterns = self.patterns_for(labels, rel_types)
            used_rels = {p.type for p in patterns} | (rel_types or set())
            label_items = [self.labels[name] for name in sorted(labels or set()) if name in self.labels]
            rel_items = [self.relationships[name] for name in sorted(used_rels) if name in self.relationships]

        lines = ["# Node labels"]
        for label in label_items:
            count = f" ({label.count:,} nodes)" if label.count is not None else ""
            lines.append(f"- :{label.name}{count}" + (f" - {label.description}" if label.description else ""))
            lines.extend(_properties_text(label.properties))
        lines.append("# Relationship types")
        for rel in rel_items:
            lines.append(f"- :{rel.name}" + (f" - {rel.description}" if rel.description else ""))
            lines.extend(_properties_text(rel.properties))
        lines.append("# Patterns (direction matters)")
        lines.extend(f"- {p}" for p in patterns)
        shown = {item.name for item in label_items} | {item.name for item in rel_items}
        indexes = [i for i in self.indexes if set(i.labels_or_types) & shown]
        if indexes:
            lines.append("# Search indexes")
            for index in indexes:
                lines.append(
                    f"- {index.type} index `{index.name}` on {index.entity_type.lower()} "
                    f":{'|'.join(index.labels_or_types)}({', '.join(index.properties)})"
                )
        if self.notes:
            lines.append("# Modeling notes")
            lines.extend(f"- {note}" for note in self.notes)
        return "\n".join(lines)


def _properties_text(properties: list[PropertySchema]) -> list[str]:
    lines = []
    for prop in properties:
        text = f"    - {prop.name}: {prop.type or 'ANY'}"
        if prop.description:
            text += f" - {prop.description}"
        if prop.values:
            text += f" (values: {', '.join(repr(v) for v in prop.values)})"
        lines.append(text)
    return lines


def _normalize_type(value: str | None) -> str | None:
    # apoc.meta.data uses Java-like names, db.schema.* uses "String", "Long"... Map both to Cypher types
    mapping = {
        "STRING": "STRING",
        "LONG": "INTEGER",
        "INTEGER": "INTEGER",
        "DOUBLE": "FLOAT",
        "FLOAT": "FLOAT",
        "BOOLEAN": "BOOLEAN",
        "DATE": "DATE",
        "DATE_TIME": "DATETIME",
        "DATETIME": "DATETIME",
        "ZONEDDATETIME": "DATETIME",
        "LOCAL_DATE_TIME": "LOCAL DATETIME",
        "LOCALDATETIME": "LOCAL DATETIME",
        "TIME": "TIME",
        "LOCAL_TIME": "LOCAL TIME",
        "LOCALTIME": "LOCAL TIME",
        "DURATION": "DURATION",
        "POINT": "POINT",
        "ZONED DATETIME": "DATETIME",
        "ZONED TIME": "TIME",
    }
    if value is None:
        return None
    if value.upper().startswith("LIST<"):
        return f"LIST<{_normalize_type(value[5:-1])}>"
    key = value.upper().replace("ARRAY", "").replace("[]", "").strip()
    base = mapping.get(key, key)
    return f"LIST<{base}>" if ("ARRAY" in value.upper() or value.endswith("[]")) else base


def load_descriptions(path: Path | None = None) -> dict:
    path = path or get_settings().schema_descriptions_path
    if not path or not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _has_apoc(session: neo4j.Session) -> bool:
    try:
        session.run("RETURN apoc.version() AS v").consume()
        return True
    except neo4j.exceptions.Neo4jError:
        return False


def introspect_schema(
    driver: neo4j.Driver, *, database: str | None = None, with_counts: bool = True, with_stored_values: bool = True
) -> GraphSchema:
    settings = get_settings()
    params = {"sample": settings.schema_sample_size, "excluded": settings.schema_excluded_labels}
    with driver.session(database=database or settings.neo4j_database, default_access_mode=neo4j.READ_ACCESS) as session:
        if _has_apoc(session):
            node_query, pattern_query = APOC_NODE_PROPERTIES, APOC_PATTERNS
        else:
            logger.warning("APOC not available: falling back on db.schema.* procedures (slower on big graphs)")
            node_query, pattern_query = NATIVE_NODE_PROPERTIES, NATIVE_PATTERNS
        node_rows = session.run(node_query, params).data()
        pattern_rows = session.run(pattern_query, params).data()
        index_rows = session.run(SEARCH_INDEXES).data()
        # Labels without properties are not returned by the property queries
        all_labels = [r["label"] for r in session.run("CALL db.labels() YIELD label RETURN label").data()]
        all_rel_types = [
            r["relationshipType"]
            for r in session.run("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType").data()
        ]

        schema = GraphSchema()
        for label in all_labels:
            if label not in params["excluded"]:
                schema.labels[label] = LabelSchema(name=label)
        for row in node_rows:
            if row["label"] in schema.labels:
                schema.labels[row["label"]].properties = [
                    PropertySchema(name=p["name"], type=_normalize_type(p["type"])) for p in row["properties"] if p
                ]
        patterns = {(r["start"], r["type"], r["end"]) for r in pattern_rows}
        for rel_type in all_rel_types:
            for row in session.run(REL_TYPE_PATTERNS.format(type=rel_type), params):
                patterns.add((row["start"], rel_type, row["end"]))
            properties = session.run(REL_TYPE_PROPERTIES.format(type=rel_type), params).data()
            schema.relationships[rel_type] = RelationshipSchema(
                name=rel_type,
                properties=[PropertySchema(name=p["name"], type=_normalize_type(p["type"])) for p in properties],
            )
        schema.patterns = [Pattern(start=s, type=t, end=e) for s, t, e in sorted(patterns)]
        used_rel_types = {p.type for p in schema.patterns}
        schema.relationships = {k: v for k, v in schema.relationships.items() if k in used_rel_types}
        schema.indexes = [
            SearchIndex(
                name=r["name"],
                type=r["type"],
                entity_type=r["entityType"],
                labels_or_types=r["labelsOrTypes"] or [],
                properties=r["properties"] or [],
            )
            for r in index_rows
            if not set(r["labelsOrTypes"] or []) & set(params["excluded"])
        ]
        if with_counts:
            # Label counts come from the count store: O(1) even on huge graphs
            for label in schema.labels.values():
                label.count = session.run(f"MATCH (n:`{label.name}`) RETURN count(n) AS c").single()["c"]
        stored_values = _stored_values(session) if with_stored_values and "SchemaElement" in all_labels else {}

    _apply_descriptions(schema, load_descriptions())
    _apply_stored_values(schema, stored_values)
    return schema


def _stored_values(session: neo4j.Session) -> dict[tuple[str, str, str], list]:
    """Categorical values sampled by the semantic layer build, keyed by (kind, element name, property)."""
    values = {}
    for record in session.run(
        "MATCH (s:SchemaElement) RETURN s.kind AS kind, s.name AS name, s.properties AS properties"
    ):
        for prop in json.loads(record["properties"] or "[]"):
            if prop.get("values"):
                values[(record["kind"], record["name"], prop["name"])] = prop["values"]
    return values


def _apply_stored_values(schema: GraphSchema, values: dict[tuple[str, str, str], list]) -> None:
    for kind, items in (("label", schema.labels), ("relationship", schema.relationships)):
        for item in items.values():
            for prop in item.properties:
                prop.values = prop.values or values.get((kind, item.name, prop.name))


def _apply_descriptions(schema: GraphSchema, descriptions: dict) -> None:
    for kind, items in (("nodes", schema.labels), ("relationships", schema.relationships)):
        for name, info in (descriptions.get(kind) or {}).items():
            if name not in items:
                continue
            items[name].description = info.get("description")
            for prop in items[name].properties:
                prop_info = (info.get("properties") or {}).get(prop.name)
                if isinstance(prop_info, str):
                    prop.description = prop_info
                elif isinstance(prop_info, dict):
                    prop.description = prop_info.get("description")
                    prop.values = prop_info.get("values")
    schema.notes = list(descriptions.get("notes") or [])


class SchemaCache:
    """Introspection is expensive on big graphs: compute once, refresh on demand."""

    def __init__(self, driver: neo4j.Driver):
        self._driver = driver
        self._schema: GraphSchema | None = None
        self._lock = threading.Lock()

    def get(self) -> GraphSchema:
        with self._lock:
            if self._schema is None:
                self._schema = introspect_schema(self._driver)
            return self._schema

    def refresh(self) -> GraphSchema:
        with self._lock:
            self._schema = introspect_schema(self._driver)
            return self._schema

    def set_driver(self, driver: neo4j.Driver) -> None:
        self._driver = driver
