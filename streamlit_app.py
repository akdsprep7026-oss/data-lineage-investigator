"""Streamlit UI adapter for the Data Lineage Investigator.

Architecture B: Streamlit imports the existing DB + investigation worker
directly. It does not call FastAPI over HTTP and does not own graph logic.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional
from uuid import UUID

import streamlit as st
from dotenv import load_dotenv

from app.db.investigations import (
    create_investigation,
    get_investigation,
    list_investigations,
)
from app.streamlit_support import (
    POLL_INTERVAL_SECONDS,
    RETRIEVAL_INGEST_COMMAND,
    SANDBOX_SEED_COMMAND,
    cloud_database_url_error,
    ensure_streamlit_startup_once,
    format_confidence_pct,
    format_timestamp,
    group_evidence_by_source,
    history_item_to_summary,
    investigation_row_to_snapshot,
    is_in_progress,
    is_retrieval_ready,
    is_sandbox_warehouse_ready,
    rank_hypotheses,
    retrieval_status_label,
    root_cause_placeholder,
    runtime_config_status,
    should_poll_status,
    should_start_worker,
    start_investigation_thread,
    truncate_text,
    useful_workflow_state,
    workflow_progress_message,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

NAV_NEW = "New Investigation"
NAV_HISTORY = "History"

st.set_page_config(
    page_title="Data Lineage Investigator",
    page_icon="🔍",
    layout="wide",
)


def _init_session_state() -> None:
    if "sidebar_nav" not in st.session_state:
        st.session_state.sidebar_nav = NAV_NEW
    if "_prev_sidebar_nav" not in st.session_state:
        st.session_state._prev_sidebar_nav = st.session_state.sidebar_nav
    # Prefer current_investigation_id (S3); migrate any S2 active id once.
    if "current_investigation_id" not in st.session_state:
        legacy = st.session_state.get("active_investigation_id")
        st.session_state.current_investigation_id = legacy
    if "worker_started_ids" not in st.session_state:
        st.session_state.worker_started_ids = set()
    if "last_error" not in st.session_state:
        st.session_state.last_error = None


def _open_detail(investigation_id: str) -> None:
    """Navigate to detail without starting a worker."""
    st.session_state.current_investigation_id = investigation_id
    st.session_state.last_error = None
    st.rerun()


def _back_to_history() -> None:
    st.session_state.current_investigation_id = None
    st.session_state.sidebar_nav = NAV_HISTORY
    st.session_state._prev_sidebar_nav = NAV_HISTORY
    st.session_state.last_error = None
    st.rerun()


def _go_new_investigation() -> None:
    st.session_state.current_investigation_id = None
    st.session_state.sidebar_nav = NAV_NEW
    st.session_state._prev_sidebar_nav = NAV_NEW
    st.session_state.last_error = None
    st.rerun()


def _load_snapshot(investigation_id: str) -> Optional[dict[str, Any]]:
    row = get_investigation(investigation_id)
    if row is None:
        return None
    return investigation_row_to_snapshot(row)


def _start_new_investigation(issue_description: str) -> None:
    text = (issue_description or "").strip()
    if not text:
        st.session_state.last_error = "Issue description cannot be empty."
        return

    try:
        investigation = create_investigation(text)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to create investigation from Streamlit")
        st.session_state.last_error = (
            "Could not create the investigation. Check database connectivity."
        )
        return

    investigation_id = str(investigation.id)
    st.session_state.current_investigation_id = investigation_id
    st.session_state.sidebar_nav = NAV_NEW
    st.session_state._prev_sidebar_nav = NAV_NEW
    st.session_state.last_error = None

    # Worker starts only here — never when opening History/Detail.
    if should_start_worker(investigation_id, st.session_state.worker_started_ids):
        try:
            start_investigation_thread(text, investigation_id)
            st.session_state.worker_started_ids.add(investigation_id)
            logger.info(
                "Streamlit started investigation worker id=%s", investigation_id
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to start investigation thread id=%s", investigation_id
            )
            st.session_state.last_error = (
                "Investigation was created but the background worker "
                "failed to start. Refresh and try again."
            )
            return

    st.rerun()


def _render_runtime_status() -> None:
    """Presence-only config/retrieval summary (never shows secret values)."""
    status = runtime_config_status()
    warehouse_label = "Ready" if is_sandbox_warehouse_ready() else "Not initialized"
    st.subheader("Runtime")
    st.write(f"Sandbox warehouse: **{warehouse_label}**")
    st.write(f"Retrieval index: **{retrieval_status_label()}**")
    if not is_sandbox_warehouse_ready():
        st.warning(
            "Sandbox warehouse is not initialized. Seed it once:\n\n"
            f"`{SANDBOX_SEED_COMMAND}`"
        )
    if not is_retrieval_ready():
        st.warning(
            "Retrieval index is not initialized. Lineage search will be empty "
            "until you build it once:\n\n"
            f"`{RETRIEVAL_INGEST_COMMAND}`"
        )
    with st.expander("Environment", expanded=False):
        st.write(f"Gemini API key: {status['gemini_api_key']}")
        st.write(f"Groq API key: {status['groq_api_key']}")
        st.write(f"LLM mode: `{status['llm_mode']}`")
        st.write(f"Embedding provider: `{status['embedding_provider']}`")
        st.write(f"Langfuse: {status['langfuse']}")
        st.write(f"Database: {status['database']}")
        st.write(f"Sandbox warehouse: {status['sandbox_warehouse']}")
        st.write(f"Retrieval: {status['retrieval']}")
        st.write(f"Streamlit Cloud: {status['streamlit_cloud']}")
        st.caption(
            "Missing LLM keys fall back to offline heuristics. "
            "Langfuse is optional. Secrets are never printed here."
        )


def _render_sidebar() -> None:
    with st.sidebar:
        st.title("Data Lineage Investigator")
        st.caption("Multi-agent root-cause analysis")
        st.radio(
            "Navigate",
            options=[NAV_NEW, NAV_HISTORY],
            key="sidebar_nav",
            label_visibility="collapsed",
        )
        # Only clear detail when the user actually changes the sidebar page.
        previous = st.session_state._prev_sidebar_nav
        current = st.session_state.sidebar_nav
        if previous != current:
            st.session_state.current_investigation_id = None
            st.session_state.last_error = None
            st.session_state._prev_sidebar_nav = current
        st.divider()
        _render_runtime_status()


def _render_new_investigation_form() -> None:
    st.header("New Investigation")
    st.write(
        "Describe a data issue. The LangGraph workflow gathers lineage, SQL, "
        "data-quality, ETL, and schema evidence, then ranks hypotheses."
    )
    issue = st.text_area(
        "Issue description",
        height=160,
        placeholder=(
            "Describe the data issue to investigate, e.g. "
            "'Total Revenue by Region dashboard shows no data for the last 2 days.'"
        ),
        key="issue_description_input",
    )
    if st.button("Start Investigation", type="primary"):
        _start_new_investigation(issue)


def _render_history() -> None:
    st.header("History")
    st.caption("Past investigations with status and root cause.")

    try:
        rows = list_investigations()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to list investigations for Streamlit history")
        st.error("Could not load investigation history.")
        return

    items = [history_item_to_summary(row) for row in rows]
    if not items:
        st.info("No investigations yet. Start one from New Investigation.")
        if st.button("Go to New Investigation"):
            _go_new_investigation()
        return

    for item in items:
        with st.container(border=True):
            top = st.columns([2, 5, 2])
            top[0].markdown(f"**{item['status']}**")
            top[1].write(truncate_text(item["issue_description"], 160))
            if top[2].button("Open", key=f"open_{item['id']}"):
                _open_detail(item["id"])
            st.caption(
                f"ID `{item['id']}` · Created {format_timestamp(item['created_at'])} "
                f"· Updated {format_timestamp(item['updated_at'])}"
            )
            if item.get("final_root_cause"):
                st.caption(
                    f"Root cause: {truncate_text(item['final_root_cause'], 140)}"
                )


def _render_hypotheses(hypotheses: list[dict[str, Any]]) -> None:
    st.subheader("Hypotheses")
    ranked = rank_hypotheses(hypotheses)
    if not ranked:
        st.info("No hypotheses recorded yet.")
        return

    for index, hypothesis in enumerate(ranked, start=1):
        if not isinstance(hypothesis, dict):
            st.warning(f"Skipping malformed hypothesis at rank {index}.")
            continue
        description = hypothesis.get("description") or "(no description)"
        try:
            confidence = float(hypothesis.get("confidence_score") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        supporting = hypothesis.get("supporting_evidence") or []
        with st.container(border=True):
            st.markdown(
                f"**#{index}** — confidence "
                f"`{format_confidence_pct(confidence)}` ({confidence:.2f})"
            )
            st.write(description)
            st.progress(max(0.0, min(1.0, confidence)))
            if supporting:
                st.caption(
                    "Supporting sources: "
                    + ", ".join(str(item) for item in supporting)
                )


def _render_evidence(evidence: list[dict[str, Any]]) -> None:
    st.subheader("Evidence")
    groups = group_evidence_by_source(evidence)
    if not groups:
        st.info("No evidence recorded yet.")
        return

    for source, items in groups:
        st.markdown(f"**Source: `{source}`**")
        for item in items:
            if not isinstance(item, dict):
                st.warning("Skipping malformed evidence item.")
                continue
            finding = item.get("finding") or "(no finding)"
            try:
                confidence = float(item.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            with st.container(border=True):
                st.write(finding)
                st.caption(
                    f"Confidence: {format_confidence_pct(confidence)} "
                    f"({confidence:.2f})"
                )


def _render_root_cause(snapshot: dict[str, Any]) -> None:
    st.subheader("Final Root Cause")
    root = snapshot.get("final_root_cause")
    if root:
        st.success(root)
        return
    status = snapshot.get("status")
    message = root_cause_placeholder(status)
    if is_in_progress(status):
        st.info(message)
    elif status == "needs_human_review":
        st.warning(message)
    else:
        st.info(message)


def _render_workflow_state(snapshot: dict[str, Any]) -> None:
    compact = useful_workflow_state(snapshot.get("workflow_state"))
    if not compact:
        return
    with st.expander("Workflow state", expanded=False):
        st.json(compact)


def _render_detail(snapshot: dict[str, Any]) -> None:
    status = snapshot["status"]

    cols = st.columns([1, 1, 1])
    cols[0].metric("Status", status)
    cols[1].write(f"**Created**\n\n{format_timestamp(snapshot['created_at'])}")
    cols[2].write(f"**Updated**\n\n{format_timestamp(snapshot['updated_at'])}")

    st.caption(f"Investigation ID: `{snapshot['id']}`")
    st.subheader("Issue description")
    st.write(snapshot["issue_description"])

    if is_in_progress(status):
        message = workflow_progress_message(snapshot.get("workflow_state"))
        with st.status(message, expanded=True, state="running"):
            st.write("Investigation is running.")
            st.write(
                "Agents update evidence and hypotheses as the LangGraph "
                "workflow progresses. This view refreshes from the database."
            )
    elif status == "needs_human_review":
        st.warning(
            "This investigation needs human review. Available evidence, "
            "hypotheses, and any root-cause text are shown below."
        )
    elif status == "resolved":
        st.success("Investigation resolved.")

    st.divider()
    _render_root_cause(snapshot)
    st.divider()
    _render_hypotheses(list(snapshot.get("hypotheses") or []))
    st.divider()
    _render_evidence(list(snapshot.get("evidence") or []))
    _render_workflow_state(snapshot)

    st.divider()
    nav_cols = st.columns(2)
    if nav_cols[0].button("Back to History"):
        _back_to_history()
    if nav_cols[1].button("New Investigation"):
        _go_new_investigation()


def _render_detail_page() -> None:
    investigation_id = st.session_state.current_investigation_id
    if not investigation_id:
        st.warning("No investigation selected.")
        if st.button("Back to History", key="detail_missing_back"):
            _back_to_history()
        return

    try:
        UUID(str(investigation_id))
    except (TypeError, ValueError):
        st.error("Stored investigation id is invalid.")
        st.session_state.current_investigation_id = None
        if st.button("Back to History", key="detail_invalid_back"):
            _back_to_history()
        return

    try:
        snapshot = _load_snapshot(str(investigation_id))
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load investigation id=%s", investigation_id)
        st.error("Could not load the investigation from the database.")
        if st.button("Back to History", key="detail_load_error_back"):
            _back_to_history()
        return

    if snapshot is None:
        st.error("Investigation not found.")
        st.session_state.current_investigation_id = None
        if st.button("Back to History", key="detail_not_found_back"):
            _back_to_history()
        return

    # Opening detail (including from History / polling) must NOT start a
    # worker. Workers start only from New Investigation create.
    st.header("Investigation")
    _render_detail(snapshot)

    if should_poll_status(snapshot["status"]):
        time.sleep(POLL_INTERVAL_SECONDS)
        st.rerun()


def main() -> None:
    # 1–2. Env already loaded via load_dotenv; validate Cloud DATABASE_URL.
    cloud_error = cloud_database_url_error()
    if cloud_error:
        st.error(cloud_error)
        st.stop()

    # 3–5. Once per process: reaper → sandbox warehouse → Chroma (skip if ready).
    bootstrap = ensure_streamlit_startup_once()
    warehouse_status = bootstrap.get("warehouse", "")
    retrieval_status = bootstrap.get("retrieval", "")
    if warehouse_status.startswith("error"):
        st.error(
            "Sandbox warehouse setup failed. "
            f"{bootstrap.get('warehouse_error') or warehouse_status}\n\n"
            f"Try `{SANDBOX_SEED_COMMAND}`."
        )
        st.stop()
    if retrieval_status.startswith("error"):
        st.error(
            "Retrieval (Chroma) setup failed. "
            f"{bootstrap.get('retrieval_error') or retrieval_status}\n\n"
            f"Try `{RETRIEVAL_INGEST_COMMAND}` with EMBEDDING_PROVIDER=onnx."
        )
        st.stop()

    # 6. Render UI.
    _init_session_state()
    _render_sidebar()

    if st.session_state.last_error:
        st.error(st.session_state.last_error)

    # Detail takes precedence when an investigation is selected.
    if st.session_state.current_investigation_id:
        _render_detail_page()
    elif st.session_state.sidebar_nav == NAV_HISTORY:
        _render_history()
    else:
        _render_new_investigation_form()


if __name__ == "__main__":
    main()
