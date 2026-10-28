"""Convert every value the Neo4j driver can return into JSON-safe Python objects.

Graph entities keep a `_type` marker so clients can tell them apart from plain maps:
    node         -> {"_type": "node", "elementId", "labels", "properties"}
    relationship -> {"_type": "relationship", "elementId", "type", "startNodeElementId", "endNodeElementId", "properties"}
    path         -> {"_type": "path", "nodes": [...], "relationships": [...]}
    point        -> {"_type": "point", "srid", "x", "y"[, "z"]} (+ "longitude"/"latitude"[/"height"] for WGS-84)
Temporal values become ISO-8601 strings, durations ISO-8601 durations, bytes base64 strings, vectors float lists.
"""

import base64
import datetime as dt
import math
from collections.abc import Mapping
from typing import Any

from neo4j import time as neo4j_time
from neo4j.graph import Node, Path, Relationship
from neo4j.spatial import Point, WGS84Point
from neo4j.vector import Vector

_TEMPORAL_TYPES = (neo4j_time.Date, neo4j_time.DateTime, neo4j_time.Time, dt.date, dt.datetime, dt.time)


def serialize_node(node: Node) -> dict:
    return {
        "_type": "node",
        "elementId": node.element_id,
        "labels": sorted(node.labels),
        "properties": {k: serialize_value(v) for k, v in node.items()},
    }


def serialize_relationship(rel: Relationship) -> dict:
    return {
        "_type": "relationship",
        "elementId": rel.element_id,
        "type": rel.type,
        "startNodeElementId": rel.start_node.element_id if rel.start_node is not None else None,
        "endNodeElementId": rel.end_node.element_id if rel.end_node is not None else None,
        "properties": {k: serialize_value(v) for k, v in rel.items()},
    }


def serialize_path(path: Path) -> dict:
    return {
        "_type": "path",
        "nodes": [serialize_node(n) for n in path.nodes],
        "relationships": [serialize_relationship(r) for r in path.relationships],
    }


def serialize_point(point: Point) -> dict:
    out: dict[str, Any] = {"_type": "point", "srid": point.srid, "x": point[0], "y": point[1]}
    if len(point) > 2:
        out["z"] = point[2]
    if isinstance(point, WGS84Point):
        out["longitude"], out["latitude"] = point[0], point[1]
        if len(point) > 2:
            out["height"] = point[2]
    return out


def serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN / Infinity are valid Cypher floats but not valid JSON
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Node):
        return serialize_node(value)
    if isinstance(value, Relationship):
        return serialize_relationship(value)
    if isinstance(value, Path):
        return serialize_path(value)
    if isinstance(value, Point):
        return serialize_point(value)
    if isinstance(value, neo4j_time.Duration):
        return value.iso_format()
    if isinstance(value, _TEMPORAL_TYPES):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, Vector):
        return [serialize_value(v) for v in value.to_native()]
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, Mapping):
        return {str(k): serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [serialize_value(v) for v in value]
    return str(value)


class GraphCollector:
    """Accumulates every node / relationship found in a result (also nested in lists, maps and paths) for visualization."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.relationships: dict[str, dict] = {}

    def collect(self, value: Any) -> None:
        if isinstance(value, Node):
            self._add_node(value)
        elif isinstance(value, Relationship):
            self._add_relationship(value)
        elif isinstance(value, Path):
            for node in value.nodes:
                self._add_node(node)
            for rel in value.relationships:
                self._add_relationship(rel)
        elif isinstance(value, Mapping):
            for v in value.values():
                self.collect(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                self.collect(v)

    def _add_node(self, node: Node) -> None:
        current = self.nodes.get(node.element_id)
        # A node only seen as a relationship endpoint has no labels/properties: replace it when the full node shows up
        if current is None or (not current["labels"] and node.labels):
            self.nodes[node.element_id] = serialize_node(node)

    def _add_relationship(self, rel: Relationship) -> None:
        if rel.element_id in self.relationships:
            return
        self.relationships[rel.element_id] = serialize_relationship(rel)
        for endpoint in (rel.start_node, rel.end_node):
            if endpoint is not None and endpoint.element_id not in self.nodes:
                self.nodes[endpoint.element_id] = serialize_node(endpoint)

    def to_dict(self) -> dict:
        return {"nodes": list(self.nodes.values()), "relationships": list(self.relationships.values())}


def compact_for_llm(value: Any, max_list: int = 20, max_str: int = 300) -> Any:
    """Shrink a serialized value for the LLM context: long lists (e.g. embeddings) and long strings are truncated."""
    if isinstance(value, dict):
        if value.get("_type") == "node":
            return {"labels": value["labels"], **compact_for_llm(value["properties"], max_list, max_str)}
        if value.get("_type") == "relationship":
            return {"type": value["type"], **compact_for_llm(value["properties"], max_list, max_str)}
        if value.get("_type") == "path":
            return {
                "path": [compact_for_llm(n, max_list, max_str) for n in value["nodes"]],
                "relationships": [r["type"] for r in value["relationships"]],
            }
        return {k: compact_for_llm(v, max_list, max_str) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > max_list:
            head = [compact_for_llm(v, max_list, max_str) for v in value[:5]]
            return head + [f"... {len(value) - 5} more items"]
        return [compact_for_llm(v, max_list, max_str) for v in value]
    if isinstance(value, str) and len(value) > max_str:
        return value[:max_str] + "..."
    return value
