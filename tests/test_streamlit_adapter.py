"""Lightweight tests for the Streamlit adapter helpers (no browser)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.db.models import InvestigationStatus
from app.streamlit_support import (
    PENDING_SIDEBAR_NAV_KEY,
    RETRIEVAL_INGEST_COMMAND,
    SIDEBAR_NAV_KEY,
    apply_pending_sidebar_nav,
    ensure_startup_reaper_once,
    format_confidence_pct,
    group_evidence_by_source,
    history_item_to_summary,
    investigation_row_to_snapshot,
    is_in_progress,
    is_retrieval_ready,
    queue_sidebar_nav,
    rank_hypotheses,
    reset_startup_reaper_for_tests,
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


def test_should_start_worker_blocks_duplicates():
    assert should_start_worker("abc", []) is True
    assert should_start_worker("abc", {"abc"}) is False
    assert should_start_worker("abc", ["xyz"]) is True


def test_queue_sidebar_nav_does_not_mutate_widget_key_until_applied():
    """Regression: button handlers must not assign sidebar_nav after radio exists.

    Streamlit raises StreamlitAPIException if session_state[widget_key] is
    written after the widget that owns that key has been instantiated.
    """
    state: dict = {SIDEBAR_NAV_KEY: "New Investigation", "_prev_sidebar_nav": "New Investigation"}
    queue_sidebar_nav(state, "History")
    assert state[SIDEBAR_NAV_KEY] == "New Investigation"
    assert state[PENDING_SIDEBAR_NAV_KEY] == "History"
    assert apply_pending_sidebar_nav(state) == "History"
    assert state[SIDEBAR_NAV_KEY] == "History"
    assert state["_prev_sidebar_nav"] == "History"
    assert PENDING_SIDEBAR_NAV_KEY not in state
    assert apply_pending_sidebar_nav(state) is None


def test_is_in_progress_matches_existing_statuses():
    assert is_in_progress("pending") is True
    assert is_in_progress("investigating") is True
    assert is_in_progress("resolved") is False
    assert is_in_progress("needs_human_review") is False
    assert is_in_progress(None) is False


def test_should_poll_status_only_while_active():
    assert should_poll_status("pending") is True
    assert should_poll_status("investigating") is True
    assert should_poll_status("resolved") is False
    assert should_poll_status("needs_human_review") is False


def test_rank_hypotheses_orders_by_confidence():
    ranked = rank_hypotheses(
        [
            {"description": "low", "confidence_score": 0.2},
            {"description": "high", "confidence_score": 0.9},
            {"description": "mid", "confidence_score": 0.5},
        ]
    )
    assert [item["description"] for item in ranked] == ["high", "mid", "low"]


def test_group_evidence_by_source_preserves_order():
    groups = group_evidence_by_source(
        [
            {"source": "lineage", "finding": "a", "confidence": 0.8},
            {"source": "sql", "finding": "b", "confidence": 0.7},
            {"source": "lineage", "finding": "c", "confidence": 0.6},
        ]
    )
    assert [source for source, _ in groups] == ["lineage", "sql"]
    assert len(groups[0][1]) == 2


def test_format_confidence_pct():
    assert format_confidence_pct(0.8) == "80%"
    assert format_confidence_pct(0.0) == "0%"
    assert format_confidence_pct("bad") == "0%"


def test_root_cause_placeholder_by_status():
    assert "in progress" in root_cause_placeholder("investigating").lower()
    assert "human review" in root_cause_placeholder(
        InvestigationStatus.NEEDS_HUMAN_REVIEW.value
    ).lower()
    assert "no final root cause" in root_cause_placeholder("resolved").lower()


def test_truncate_text():
    assert truncate_text("short") == "short"
    assert truncate_text("abcdefghij", max_chars=5) == "abcde…"


def test_useful_workflow_state_filters_noise():
    assert useful_workflow_state(None) is None
    assert useful_workflow_state({}) is None
    compact = useful_workflow_state(
        {
            "current_node": "validation",
            "retry_count": 1,
            "agents_to_run": [],
            "noise": "ignored",
        }
    )
    assert compact == {"current_node": "validation", "retry_count": 1}


def test_history_and_snapshot_helpers_use_existing_fields():
    inv_id = uuid4()
    row = SimpleNamespace(
        id=inv_id,
        issue_description="Revenue is stale",
        status=InvestigationStatus.RESOLVED,
        evidence=[{"source": "etl", "finding": "job failed", "confidence": 0.9}],
        hypotheses=[
            {
                "description": "pipeline stalled",
                "supporting_evidence": ["etl"],
                "confidence_score": 0.9,
            }
        ],
        workflow_state={"current_node": "human_review", "retry_count": 0},
        final_root_cause="pipeline stalled",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:01:00Z",
    )
    summary = history_item_to_summary(row)
    assert summary["id"] == str(inv_id)
    assert summary["status"] == "resolved"
    assert summary["issue_description"] == "Revenue is stale"

    snapshot = investigation_row_to_snapshot(row)
    assert snapshot["final_root_cause"] == "pipeline stalled"
    assert snapshot["evidence"][0]["source"] == "etl"
    assert snapshot["hypotheses"][0]["confidence_score"] == 0.9


def test_workflow_progress_message_uses_current_node():
    assert "manager" in workflow_progress_message({"current_node": "manager"})
    assert "boom" in workflow_progress_message({"failed": True, "error": "boom"})


def test_ensure_startup_reaper_once_calls_reaper_only_once():
    reset_startup_reaper_for_tests()
    with patch(
        "app.streamlit_support.reap_stale_investigations", return_value=2
    ) as reaper:
        ensure_startup_reaper_once()
        ensure_startup_reaper_once()
        assert reaper.call_count == 1
    reset_startup_reaper_for_tests()


def test_start_investigation_thread_calls_shared_worker():
    mock_thread = MagicMock()
    with (
        patch("app.streamlit_support.run_investigation_worker") as worker,
        patch(
            "app.streamlit_support.threading.Thread", return_value=mock_thread
        ) as thread_cls,
    ):
        started = start_investigation_thread("issue text", "inv-1")
        assert started is mock_thread
        thread_cls.assert_called_once()
        kwargs = thread_cls.call_args.kwargs
        assert kwargs["args"] == ("issue text", "inv-1")
        assert kwargs["target"] is worker
        mock_thread.start.assert_called_once()


def test_opening_history_detail_must_not_bypass_should_start_worker():
    """Regression guard: History/Detail reuse should_start_worker only.

    Selecting an investigation must not invent a second dispatch path.
    """
    assert should_start_worker("already-running", {"already-running"}) is False


def test_is_retrieval_ready_false_when_persist_dir_missing(tmp_path, monkeypatch):
    missing = tmp_path / "no_chroma"
    monkeypatch.setattr("app.streamlit_support.CHROMA_PERSIST_DIR", missing)
    assert is_retrieval_ready() is False
    assert retrieval_status_label() == "Not initialized"


def test_is_retrieval_ready_false_when_collection_empty(monkeypatch):
    class _EmptyCollection:
        def count(self):
            return 0

    class _Client:
        def get_collection(self, name):
            assert name
            return _EmptyCollection()

    class _Path:
        def exists(self):
            return True

    monkeypatch.setattr("app.streamlit_support.CHROMA_PERSIST_DIR", _Path())
    monkeypatch.setattr(
        "app.streamlit_support.get_chroma_client", lambda: _Client()
    )
    assert is_retrieval_ready() is False


def test_is_retrieval_ready_true_when_collection_has_docs(monkeypatch):
    class _Collection:
        def count(self):
            return 7

    class _Client:
        def get_collection(self, name):
            return _Collection()

    class _Path:
        def exists(self):
            return True

    monkeypatch.setattr("app.streamlit_support.CHROMA_PERSIST_DIR", _Path())
    monkeypatch.setattr(
        "app.streamlit_support.get_chroma_client", lambda: _Client()
    )
    assert is_retrieval_ready() is True
    assert retrieval_status_label() == "Ready"


def test_runtime_config_status_never_includes_secret_values(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "real-looking-key")
    monkeypatch.setenv("GROQ_API_KEY", "your-groq-api-key-here")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "your-langfuse-public-key-here")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "your-langfuse-secret-key-here")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "onnx")

    status = runtime_config_status()
    blob = " ".join(status.values())
    assert "real-looking-key" not in blob
    assert "your-groq" not in blob
    assert status["gemini_api_key"] == "configured"
    assert status["groq_api_key"] == "not configured"
    assert status["langfuse"] == "not configured"
    assert status["database"] == "local embedded"
    assert status["embedding_provider"] == "onnx"
    assert "retrieval" in status
    assert "sandbox_warehouse" in status
    assert status["streamlit_cloud"] in {"detected", "local/other"}
    assert RETRIEVAL_INGEST_COMMAND.startswith("python -m")


def test_rank_and_group_helpers_tolerate_malformed_entries():
    ranked = rank_hypotheses(
        [
            {"description": "ok", "confidence_score": 0.5},
            "not-a-dict",  # type: ignore[list-item]
            {"description": "missing score"},
        ]
    )
    assert ranked[0]["description"] == "ok"
    groups = group_evidence_by_source(
        [
            {"source": "etl", "finding": "a", "confidence": 0.9},
            "bad",  # type: ignore[list-item]
            {"finding": "no source", "confidence": 0.1},
        ]
    )
    sources = [source for source, _ in groups]
    assert "etl" in sources
    assert "unknown" in sources


def test_snapshot_helpers_tolerate_empty_collections():
    row = SimpleNamespace(
        id=uuid4(),
        issue_description="empty result",
        status=InvestigationStatus.NEEDS_HUMAN_REVIEW,
        evidence=None,
        hypotheses=None,
        workflow_state=None,
        final_root_cause=None,
        created_at=None,
        updated_at=None,
    )
    snapshot = investigation_row_to_snapshot(row)
    assert snapshot["evidence"] == []
    assert snapshot["hypotheses"] == []
    assert snapshot["workflow_state"] == {}
    assert snapshot["status"] == "needs_human_review"
    assert root_cause_placeholder(snapshot["status"]).lower().find("human") >= 0


def test_mark_investigation_failed_sets_needs_human_review_and_preserves_work():
    from app.db.investigations import (
        create_investigation,
        get_investigation,
        update_investigation,
    )
    from app.investigation_runner import mark_investigation_failed

    investigation = create_investigation("Streamlit adapter failure path")
    update_investigation(
        investigation.id,
        status=InvestigationStatus.INVESTIGATING,
        add_evidence={
            "source": "etl_agent",
            "finding": "prior finding",
            "confidence": 0.7,
        },
        add_hypothesis={
            "description": "prior hypothesis",
            "supporting_evidence": ["etl_agent"],
            "confidence_score": 0.6,
        },
        workflow_state={"current_node": "validation", "retry_count": 1},
    )

    mark_investigation_failed(str(investigation.id), RuntimeError("worker boom"))

    updated = get_investigation(investigation.id)
    assert updated is not None
    assert updated.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert len(updated.evidence) == 2
    assert updated.evidence[0]["source"] == "etl_agent"
    assert updated.evidence[1]["source"] == "system"
    assert "worker boom" in updated.evidence[1]["finding"]
    assert updated.hypotheses[0]["description"] == "prior hypothesis"
    assert updated.workflow_state["failed"] is True
    assert updated.workflow_state["retry_count"] == 1
    assert updated.workflow_state["current_node"] == "background_failure"


def test_run_investigation_worker_failure_does_not_raise():
    from app.db.investigations import create_investigation, get_investigation
    from app.investigation_runner import run_investigation_worker

    investigation = create_investigation("worker exception must stay contained")
    with patch(
        "app.investigation_runner.run_investigation",
        side_effect=RuntimeError("simulated crash"),
    ):
        # Must not propagate into the Streamlit/uvicorn host process.
        run_investigation_worker(
            investigation.issue_description, str(investigation.id)
        )

    updated = get_investigation(investigation.id)
    assert updated is not None
    assert updated.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert any(item.get("source") == "system" for item in updated.evidence)
