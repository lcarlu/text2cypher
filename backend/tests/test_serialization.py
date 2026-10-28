import datetime as dt
import json
import math

from neo4j import time as neo4j_time
from neo4j.spatial import CartesianPoint, WGS84Point

from text2cypher_backend.graph.serialization import compact_for_llm, serialize_value


def test_scalars_and_collections():
    value = {"a": 1, "b": [1.5, None, True, "x"], "c": {"d": b"\x00\x01"}}
    assert serialize_value(value) == {"a": 1, "b": [1.5, None, True, "x"], "c": {"d": "AAE="}}


def test_non_finite_floats_are_json_safe():
    out = serialize_value([math.nan, math.inf])
    json.dumps(out, allow_nan=False)


def test_temporal_values():
    assert serialize_value(neo4j_time.Date(2024, 1, 31)) == "2024-01-31"
    assert serialize_value(dt.date(2024, 1, 31)) == "2024-01-31"
    assert serialize_value(neo4j_time.Duration(years=1, days=2)) == "P1Y2D"
    assert serialize_value(neo4j_time.DateTime(2024, 1, 31, 10, 0, 0, tzinfo=dt.UTC)).startswith("2024-01-31T10:00:00")


def test_points():
    assert serialize_value(CartesianPoint((1, 2))) == {"_type": "point", "srid": 7203, "x": 1, "y": 2}
    wgs = serialize_value(WGS84Point((2.35, 48.85)))
    assert wgs["longitude"] == 2.35 and wgs["latitude"] == 48.85 and wgs["srid"] == 4326


def test_compact_for_llm_truncates_long_lists_and_strings():
    node = {"_type": "node", "labels": ["Doc"], "properties": {"embedding": list(range(1536)), "text": "x" * 1000}}
    compact = compact_for_llm(node)
    assert compact["labels"] == ["Doc"]
    assert len(compact["embedding"]) == 6 and compact["embedding"][-1] == "... 1531 more items"
    assert len(compact["text"]) == 303
