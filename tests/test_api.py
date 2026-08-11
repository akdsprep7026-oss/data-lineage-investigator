"""HTTP tests for the Step 10 investigations API."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.main import _mark_investigation_failed, _run_investigation_background, app
from app.db.investigations import (
    create_investigation,
    get_investigation,
    update_investigation,
)
from app.db.models import InvestigationStatus
from app.graph.nodes import human_review_node

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_post_investigation_returns_immediately_and_schedules_background_run():
    with patch("app.api.main._run_investigation_background") as background:
        # TestClient runs BackgroundTasks inline after the response is
        # built; patching the runner keeps the graph offline in this test.
        response = client.post(
            "/investigations",
            json={"issue_description": "Revenue looks low for 2024-01-20"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert "id" in body
    background.assert_called_once()
    args, _kwargs = background.call_args
    assert args[0] == "Revenue looks low for 2024-01-20"
    assert args[1] == body["id"]


def test_get_investigation_detail_and_history():
    investigation = create_investigation("Dashboard metric is empty")
    update_investigation(
        investigation.id,
        status=InvestigationStatus.RESOLVED,
        add_evidence={
            "source": "etl_agent",
            "finding": "job failed",
            "confidence": 0.9,
        },
        add_hypothesis={
            "description": "pipeline stalled",
            "supporting_evidence": ["etl_agent"],
            "confidence_score": 0.9,
        },
        final_root_cause="pipeline stalled",
    )

    detail = client.get(f"/investigations/{investigation.id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["id"] == str(investigation.id)
    assert payload["status"] == "resolved"
    assert payload["final_root_cause"] == "pipeline stalled"
    assert len(payload["evidence"]) == 1
    assert len(payload["hypotheses"]) == 1

    history = client.get("/investigations")
    assert history.status_code == 200
    ids = [item["id"] for item in history.json()]
    assert str(investigation.id) in ids


def test_get_investigation_detail_404_for_unknown_id():
    response = client.get(f"/investigations/{uuid4()}")
    assert response.status_code == 404


def test_background_exception_marks_needs_human_review_and_preserves_prior_work():
    investigation = create_investigation("Revenue looks wrong after pipeline change")
    update_investigation(
        investigation.id,
        status=InvestigationStatus.INVESTIGATING,
        add_evidence={
            "source": "etl_agent",
            "finding": "build_fct_daily_revenue last_run_status=failed",
            "confidence": 0.85,
        },
        add_hypothesis={
            "description": "stale pipeline job",
            "supporting_evidence": ["etl_agent"],
            "confidence_score": 0.8,
        },
        workflow_state={"current_node": "validation", "retry_count": 1},
    )

    with patch(
        "app.api.main.run_investigation",
        side_effect=RuntimeError("simulated graph crash"),
    ):
        _run_investigation_background(
            investigation.issue_description, str(investigation.id)
        )

    updated = get_investigation(investigation.id)
    assert updated is not None
    assert updated.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert updated.status != InvestigationStatus.INVESTIGATING
    assert len(updated.evidence) == 2
    assert updated.evidence[0]["source"] == "etl_agent"
    assert updated.evidence[1]["source"] == "system"
    assert "simulated graph crash" in updated.evidence[1]["finding"]
    assert len(updated.hypotheses) == 1
    assert updated.hypotheses[0]["description"] == "stale pipeline job"
    assert updated.workflow_state["current_node"] == "background_failure"
    assert updated.workflow_state["failed"] is True
    assert updated.workflow_state["retry_count"] == 1
    assert updated.workflow_state["error_type"] == "RuntimeError"


def test_mark_investigation_failed_helper_sets_terminal_status():
    investigation = create_investigation("placeholder issue")
    update_investigation(
        investigation.id, status=InvestigationStatus.INVESTIGATING
    )

    _mark_investigation_failed(str(investigation.id), ValueError("boom"))

    updated = get_investigation(investigation.id)
    assert updated is not None
    assert updated.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert any(item["source"] == "system" for item in updated.evidence)


def test_successful_background_run_keeps_human_review_terminal_status():
    investigation = create_investigation("Confidently supported stale pipeline")

    def _fake_run(issue_description, *, investigation_id=None):
        update_investigation(
            investigation_id,
            status=InvestigationStatus.INVESTIGATING,
            add_evidence={
                "source": "validation",
                "finding": "job failure confirmed",
                "confidence": 0.9,
            },
        )
        result = human_review_node(
            {
                "investigation_id": investigation_id,
                "issue_description": issue_description,
                "top_hypothesis": {
                    "description": "build_fct_daily_revenue keeps failing",
                    "supporting_evidence": ["etl_agent"],
                    "confidence_score": 0.95,
                },
                "validation": {
                    "confirmed": True,
                    "claim_kind": "stale_pipeline",
                    "checked": "pipeline_jobs.json",
                    "note": "job failed",
                    "gap": None,
                },
                "retry_count": 0,
                "agents_to_run": [],
                "agents_completed": [],
                "validation_notes": [],
                "evidence": [],
                "hypotheses": [],
            }
        )
        return result

    with patch("app.api.main.run_investigation", side_effect=_fake_run):
        _run_investigation_background(
            investigation.issue_description, str(investigation.id)
        )

    updated = get_investigation(investigation.id)
    assert updated is not None
    assert updated.status == InvestigationStatus.RESOLVED
    assert updated.final_root_cause == "build_fct_daily_revenue keeps failing"


def test_background_run_forces_terminal_when_graph_returns_non_terminal_status():
    investigation = create_investigation("Non-terminal graph return")
    update_investigation(
        investigation.id,
        status=InvestigationStatus.INVESTIGATING,
        workflow_state={"current_node": "validation", "retry_count": 1},
        add_evidence={
            "source": "lineage",
            "finding": "prior finding",
            "confidence": 0.5,
        },
    )

    with patch(
        "app.api.main.run_investigation",
        return_value={
            "status": InvestigationStatus.INVESTIGATING.value,
            "retry_count": 1,
            "validation_pass_count": 2,
        },
    ):
        _run_investigation_background(
            investigation.issue_description, str(investigation.id)
        )

    updated = get_investigation(investigation.id)
    assert updated is not None
    assert updated.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert updated.workflow_state["retry_count"] == 1
    assert any(item["source"] == "lineage" for item in updated.evidence)
    assert any(item["source"] == "system" for item in updated.evidence)


def test_low_confidence_background_run_ends_in_needs_human_review():
    investigation = create_investigation("Weak hypothesis case")

    def _fake_run(issue_description, *, investigation_id=None):
        result = human_review_node(
            {
                "investigation_id": investigation_id,
                "issue_description": issue_description,
                "top_hypothesis": {
                    "description": "unclear root cause",
                    "supporting_evidence": [],
                    "confidence_score": 0.4,
                },
                "validation": {
                    "confirmed": False,
                    "claim_kind": "unknown",
                    "checked": "none",
                    "note": "could not confirm",
                    "gap": "unknown",
                },
                "retry_count": 2,
                "validation_pass_count": 3,
                "agents_to_run": [],
                "agents_completed": [],
                "validation_notes": [],
                "evidence": [],
                "hypotheses": [],
            }
        )
        return result

    with patch("app.api.main.run_investigation", side_effect=_fake_run):
        _run_investigation_background(
            investigation.issue_description, str(investigation.id)
        )

    updated = get_investigation(investigation.id)
    assert updated is not None
    assert updated.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert updated.final_root_cause is None
    assert any(item["source"] == "system" for item in updated.evidence)
