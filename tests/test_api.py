"""HTTP tests for the Step 10 investigations API."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.main import app
from app.db.investigations import create_investigation, update_investigation
from app.db.models import InvestigationStatus

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
