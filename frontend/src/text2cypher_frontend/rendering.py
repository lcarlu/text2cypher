"""Turn API results into a pandas table and a neo4j-viz graph."""

import json
from typing import Any

import pandas as pd
from neo4j_viz import Node, Relationship, VisualizationGraph

# Properties used, in order, as node caption
CAPTION_PROPERTIES = ["name", "title", "firstName", "code", "city", "email", "id"]
MAX_GRAPH_NODES = 2000


def _cell(value: Any) -> Any:
    """Readable value for a table cell: nodes as (:Label {...}), relationships as [:TYPE {...}]."""
    if isinstance(value, dict):
        kind = value.get("_type")
        if kind == "node":
            return f"(:{':'.join(value['labels'])} {json.dumps(value['properties'], ensure_ascii=False)})"
        if kind == "relationship":
            return f"[:{value['type']} {json.dumps(value['properties'], ensure_ascii=False)}]"
        if kind == "path":
            hops = [f"(:{':'.join(n['labels'])})" for n in value["nodes"]]
            return "-".join(hops)
        if kind == "point":
            return f"point({value['x']}, {value['y']}" + (f", {value['z']})" if "z" in value else ")")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if any(isinstance(v, (dict, list)) for v in value):
            return json.dumps([_cell(v) for v in value], ensure_ascii=False)
        return value
    return value


def result_to_dataframe(result: dict) -> pd.DataFrame:
    rows = [{col: _cell(row.get(col)) for col in result["columns"]} for row in result["rows"]]
    return pd.DataFrame(rows, columns=result["columns"])


def _caption(node: dict) -> str:
    props = node.get("properties", {})
    if props.get("firstName") and props.get("lastName"):
        return f"{props['firstName']} {props['lastName']}"
    for key in CAPTION_PROPERTIES:
        if props.get(key) not in (None, ""):
            return str(props[key])
    return node["labels"][0] if node.get("labels") else ""


def build_graph_html(graph: dict, height: int, theme: str = "auto") -> str | None:
    nodes = graph.get("nodes", [])[:MAX_GRAPH_NODES]
    if not nodes:
        return None
    ids = {n["elementId"] for n in nodes}
    viz_nodes = [
        Node(
            id=n["elementId"],
            caption=_caption(n),
            properties={
                "label": (n.get("labels") or ["?"])[0],
                "labels": n.get("labels", []),
                **n.get("properties", {}),
            },
        )
        for n in nodes
    ]
    viz_rels = [
        Relationship(
            id=r["elementId"],
            source=r["startNodeElementId"],
            target=r["endNodeElementId"],
            caption=r["type"],
            properties={"type": r["type"], **{k: v for k, v in r.get("properties", {}).items() if v is not None}},
        )
        for r in graph.get("relationships", [])
        if r["startNodeElementId"] in ids and r["endNodeElementId"] in ids
    ]
    vg = VisualizationGraph(nodes=viz_nodes, relationships=viz_rels)
    vg.color_nodes(property="label")
    return vg.render(height=f"{height}px", theme=theme, max_allowed_nodes=MAX_GRAPH_NODES).data
