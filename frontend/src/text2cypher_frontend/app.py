"""Streamlit chatbot for the Text2Cypher API.

uv run text2cypher-ui        (API_BASE in the project .env, default http://127.0.0.1:8000)
"""

import json
from pathlib import Path

import streamlit as st

from text2cypher_frontend import api_client
from text2cypher_frontend.rendering import build_graph_html, result_to_dataframe

HERE = Path(__file__).resolve().parent
CONTENT_HEIGHT_PX = 640
AVATAR = {"user": None, "assistant": str(HERE / "icon" / "Neo4j-icon-color.png")}
EXAMPLE_QUESTIONS = json.loads((HERE / "data" / "example_questions.json").read_text(encoding="utf-8"))

st.set_page_config(page_title="Text2Cypher", page_icon="💬", layout="wide")

DEFAULTS = {
    "messages": [],
    "graph_html": None,
    "graph_caption": "Ask a question returning nodes or relationships to see them here, or display the data model.",
    "busy": False,
    "show_cypher": True,
    "show_result": True,
    "show_queries": False,
    "show_usage": True,
    "use_history": True,
}
for key, value in DEFAULTS.items():
    st.session_state.setdefault(key, value)

st.markdown(
    "<style>.block-container {padding-top: 1rem; padding-bottom: 0rem;}</style>",
    unsafe_allow_html=True,
)


def _theme() -> str:
    return st.context.theme.type or "auto"


def _show_graph(graph: dict | None, caption: str) -> None:
    html = build_graph_html(graph or {}, CONTENT_HEIGHT_PX - 60, _theme()) if graph else None
    if html:
        st.session_state.graph_html = html
        st.session_state.graph_caption = caption


def _queue_question(question: str) -> None:
    st.session_state.pending_prompt = question


def render_result(result: dict, key: str) -> None:
    truncated = " (truncated)" if result.get("truncated") else ""
    with st.expander(f"Result: **{result['row_count']:,} rows**{truncated}", expanded=True):
        if result["columns"]:
            df = result_to_dataframe(result)
            st.dataframe(df, width="stretch", hide_index=True, height=min(400, 38 + 35 * len(df)))
            col_csv, col_json = st.columns(2)
            col_csv.download_button(
                "CSV",
                df.to_csv(index=False),
                file_name="result.csv",
                key=f"csv_{key}",
                icon=":material/download:",
                width="stretch",
            )
            col_json.download_button(
                "JSON",
                json.dumps(result["rows"], ensure_ascii=False, indent=2),
                file_name="result.json",
                key=f"json_{key}",
                icon=":material/data_object:",
                width="stretch",
            )
        summary = result.get("summary") or {}
        st.caption(
            f"{summary.get('elapsed_ms', '?')} ms · {len(result['graph']['nodes'])} nodes / "
            f"{len(result['graph']['relationships'])} relationships in the result"
        )
        for notification in summary.get("notifications", []):
            st.warning(f"{notification.get('title')}: {notification.get('description')}")


def render_assistant(msg: dict, key: str) -> None:
    if msg.get("error"):
        st.error(msg["error"])
        return
    st.markdown(msg["content"] or "_No answer._")
    if st.session_state.show_cypher:
        if msg.get("cypher"):
            with st.expander("Cypher query", expanded=True):
                st.code(msg["cypher"], language="cypher")
        else:
            st.caption("No Cypher query answered this question.")
    if st.session_state.show_result and msg.get("result"):
        render_result(msg["result"], key)
    if st.session_state.show_queries and (msg.get("queries") or msg.get("tool_calls")):
        with st.expander(f"Queries & tools ({len(msg.get('queries', []))} queries)"):
            for query in msg.get("queries", []):
                icon = "✅" if query["status"] == "success" else "❌"
                rows = f" · {query['row_count']} rows" if query.get("row_count") is not None else ""
                st.markdown(f"{icon} **{query['tool']}** ({query['kind']}){rows}")
                st.code(query["query"], language="cypher")
                if query.get("error"):
                    st.caption(query["error"])
            st.markdown("**Tool calls**")
            st.json(msg.get("tool_calls", []), expanded=False)
            if msg.get("schema_matches"):
                st.markdown("**Semantic layer matches**")
                st.json(msg["schema_matches"], expanded=False)
    if st.session_state.show_usage and msg.get("usage"):
        seconds = (msg.get("elapsed_ms") or 0) / 1000
        with st.expander(f"Usage: **{msg['usage']['total_tokens']:,} tokens** · {seconds:.1f}s"):
            st.json(msg["usage"])


def ask(question: str) -> None:
    history = (
        [
            {"role": m["role"], "content": m["content"], "cypher": m.get("cypher")}
            for m in st.session_state.messages
            if not m.get("error")
        ]
        if st.session_state.use_history
        else []
    )
    st.session_state.messages.append({"role": "user", "content": question})
    try:
        data = api_client.chat(question, history)
        message = {"role": "assistant", "content": data["answer"], **data}
        result = data.get("result") or {}
        _show_graph(result.get("graph"), f"Result graph of: _{question}_")
    except api_client.ApiError as e:
        message = {"role": "assistant", "content": "", "error": str(e)}
    st.session_state.messages.append(message)


# ---------- Header ----------
title_col, model_col, clear_col, settings_col = st.columns([20, 0.8, 0.8, 1], vertical_alignment="bottom")
with title_col:
    st.title("Talk to your knowledge graph")
with model_col:
    if st.button("", icon=":material/graph_2:", help="Display the graph data model", width="stretch"):
        try:
            _show_graph(api_client.schema_graph(), "Graph data model (labels and relationship types)")
        except api_client.ApiError as e:
            st.toast(str(e), icon="🚨")
with clear_col:
    if st.button("", icon=":material/delete_sweep:", help="New conversation", width="stretch"):
        st.session_state.messages = []
        st.rerun()
with settings_col:
    with st.popover("⚙️", help="Settings"):
        st.toggle("Cypher query", key="show_cypher", help="Cypher query that produced the answer")
        st.toggle("Result table", key="show_result", help="Full result of the Cypher query")
        st.toggle("Queries & tools", key="show_queries", help="Every query and tool call made by the agent")
        st.toggle("Usage", key="show_usage", help="Tokens and response time")
        st.toggle(
            "Conversation memory", key="use_history", help="Send the previous messages so that follow-up questions work"
        )
        try:
            status = api_client.health()
            st.caption(
                f"API {api_client.API_BASE} · Neo4j {'🟢' if status['neo4j'] else '🔴'} · "
                f"semantic layer {'🟢' if status['semantic_layer'] else '⚪'}"
            )
        except api_client.ApiError as e:
            st.caption(f"🔴 {e}")

chat_tab, cypher_tab, schema_tab = st.tabs(["💬 Chat", "⌨️ Cypher", "📖 Schema"])

# ---------- Chat ----------
with chat_tab:
    prompt = st.chat_input("Ask a question about the knowledge graph")
    prompt = prompt or st.session_state.pop("pending_prompt", None)
    col_chat, col_graph = st.columns([3, 2], gap="medium")
    with col_chat:
        with st.container(height=CONTENT_HEIGHT_PX):
            for i, msg in enumerate(st.session_state.messages):
                with st.chat_message(msg["role"], avatar=AVATAR[msg["role"]]):
                    if msg["role"] == "user":
                        st.markdown(msg["content"])
                    else:
                        render_assistant(msg, str(i))
            if prompt:
                with st.chat_message("user"):
                    st.markdown(prompt)
                with st.chat_message("assistant", avatar=AVATAR["assistant"]):
                    with st.spinner("The agent is querying the graph…"):
                        ask(prompt)
                st.rerun()
            if not st.session_state.messages:
                st.caption("Example questions")
                for i, item in enumerate(EXAMPLE_QUESTIONS):
                    st.button(
                        item["question"],
                        key=f"example_{i}",
                        width="stretch",
                        on_click=_queue_question,
                        args=(item["question"],),
                    )
    with col_graph:
        with st.container(border=True, height=CONTENT_HEIGHT_PX):
            st.caption(st.session_state.graph_caption)
            if st.session_state.graph_html:
                st.iframe(st.session_state.graph_html, height=CONTENT_HEIGHT_PX - 60)

# ---------- Cypher console ----------
with cypher_tab:
    st.caption("Run a Cypher query directly through the API (same read-only guard-rails as the agent).")
    last_cypher = next((m.get("cypher") for m in reversed(st.session_state.messages) if m.get("cypher")), "")
    query = st.text_area(
        "Cypher",
        value=last_cypher or "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count",
        height=160,
        key="console_query",
    )
    params_col, rows_col, run_col = st.columns([4, 1, 1], vertical_alignment="bottom")
    params_text = params_col.text_input("Parameters (JSON)", value="{}")
    max_rows = rows_col.number_input("Max rows", min_value=1, max_value=100_000, value=1000)
    if run_col.button("Run", type="primary", icon=":material/play_arrow:", width="stretch"):
        try:
            result = api_client.run_cypher(query, json.loads(params_text or "{}"), int(max_rows))
            st.session_state.console_result = result
            _show_graph(result.get("graph"), "Result graph of the Cypher console")
        except (api_client.ApiError, json.JSONDecodeError) as e:
            st.session_state.console_result = None
            st.error(str(e))
    if st.session_state.get("console_result"):
        render_result(st.session_state.console_result, "console")
        if st.session_state.console_result["graph"]["nodes"]:
            st.caption("The graph is displayed in the Chat tab.")

# ---------- Schema ----------
with schema_tab:
    if st.button("Load the schema seen by the agent", icon=":material/schema:"):
        try:
            st.session_state.schema_text = api_client.schema_text()
        except api_client.ApiError as e:
            st.error(str(e))
    if st.session_state.get("schema_text"):
        st.code(st.session_state.schema_text, language="markdown")
