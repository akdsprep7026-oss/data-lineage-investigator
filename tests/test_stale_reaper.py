"""Tests for startup stale-investigation reclaim.

Covers orphaned pending/investigating rows left behind when an
in-process BackgroundTask dies (e.g. Render restart) without raising.

Assertions target the specific investigation under test: the shared
embedded Postgres may contain older pending/investigating rows from
prior suite runs, so global reclaim counts are not reliable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api.main import app
from app.db.base import get_session
from app.db.investigations import (
    DEFAULT_STALE_INVESTIGATION_MINUTES,
    create_investigation,
    get_investigation,
    get_stale_investigation_minutes,
    reap_stale_investigations,
    reclaim_stale_investigation,
    update_investigation,
)
from app.db.models import Investigation, InvestigationStatus


def _backdate_updated_at(investigation_id, *, minutes_ago: int) -> None:
    """Force updated_at into the past without triggering Column.onupdate."""
    stamped = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    session = get_session()
    try:
        session.execute(
            update(Investigation)
            .where(Investigation.id == investigation_id)
            .values(updated_at=stamped)
        )
        session.commit()
    finally:
        session.close()


def test_default_stale_threshold_is_thirty_minutes(monkeypatch):
    monkeypatch.delenv("STALE_INVESTIGATION_MINUTES", raising=False)
    assert get_stale_investigation_minutes() == 30
    assert DEFAULT_STALE_INVESTIGATION_MINUTES == 30


def test_configured_stale_threshold_is_respected(monkeypatch):
    monkeypatch.setenv("STALE_INVESTIGATION_MINUTES", "45")
    assert get_stale_investigation_minutes() == 45


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "1.5"])
def test_invalid_stale_threshold_raises(monkeypatch, raw):
    monkeypatch.setenv("STALE_INVESTIGATION_MINUTES", raw)
    with pytest.raises(ValueError, match="STALE_INVESTIGATION_MINUTES"):
        get_stale_investigation_minutes()


def test_empty_candidate_set_does_not_error():
    """Reaper must tolerate zero matches (and any pre-existing DB noise)."""
    count = reap_stale_investigations(
        stale_minutes=30,
        now=datetime.now(timezone.utc) - timedelta(days=3650),
    )
    assert isinstance(count, int)
    assert count >= 0


def test_stale_investigating_is_reclaimed_to_needs_human_review():
    investigation = create_investigation("stale investigating case")
    update_investigation(
        investigation.id,
        status=InvestigationStatus.INVESTIGATING,
        workflow_state={"current_node": "validation", "retry_count": 1},
    )
    _backdate_updated_at(investigation.id, minutes_ago=60)

    reap_stale_investigations(stale_minutes=30)

    updated = get_investigation(investigation.id)
    assert updated.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert updated.final_root_cause is None


def test_stale_pending_is_reclaimed_to_needs_human_review():
    investigation = create_investigation("stale pending case")
    _backdate_updated_at(investigation.id, minutes_ago=60)

    reap_stale_investigations(stale_minutes=30)
    assert get_investigation(investigation.id).status == (
        InvestigationStatus.NEEDS_HUMAN_REVIEW
    )


def test_recent_investigating_is_left_alone():
    investigation = create_investigation("fresh investigating")
    update_investigation(
        investigation.id, status=InvestigationStatus.INVESTIGATING
    )

    reap_stale_investigations(stale_minutes=30)
    assert get_investigation(investigation.id).status == (
        InvestigationStatus.INVESTIGATING
    )


def test_recent_pending_is_left_alone():
    investigation = create_investigation("fresh pending")

    reap_stale_investigations(stale_minutes=30)
    assert get_investigation(investigation.id).status == InvestigationStatus.PENDING


def test_resolved_investigation_is_unchanged():
    investigation = create_investigation("already resolved")
    update_investigation(
        investigation.id,
        status=InvestigationStatus.RESOLVED,
        final_root_cause="known cause",
        workflow_state={"current_node": "human_review", "retry_count": 0},
    )
    _backdate_updated_at(investigation.id, minutes_ago=120)

    reap_stale_investigations(stale_minutes=30)
    updated = get_investigation(investigation.id)
    assert updated.status == InvestigationStatus.RESOLVED
    assert updated.final_root_cause == "known cause"


def test_needs_human_review_investigation_is_unchanged():
    investigation = create_investigation("already needs review")
    update_investigation(
        investigation.id,
        status=InvestigationStatus.NEEDS_HUMAN_REVIEW,
        add_evidence={
            "source": "system",
            "finding": "prior note",
            "confidence": 1.0,
        },
    )
    _backdate_updated_at(investigation.id, minutes_ago=120)

    reap_stale_investigations(stale_minutes=30)
    updated = get_investigation(investigation.id)
    assert updated.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert len(updated.evidence) == 1


def test_stale_reclaim_preserves_evidence_hypotheses_and_merges_workflow_state():
    investigation = create_investigation("preserve prior work")
    update_investigation(
        investigation.id,
        status=InvestigationStatus.INVESTIGATING,
        add_evidence={
            "source": "etl_agent",
            "finding": "job failed",
            "confidence": 0.9,
        },
        add_hypothesis={
            "description": "pipeline stalled",
            "supporting_evidence": ["etl_agent"],
            "confidence_score": 0.8,
        },
        workflow_state={
            "current_node": "validation",
            "retry_count": 2,
            "validation_pass_count": 3,
            "follow_up_query": "row counts",
        },
    )
    _backdate_updated_at(investigation.id, minutes_ago=90)

    reap_stale_investigations(stale_minutes=30)
    updated = get_investigation(investigation.id)

    assert updated.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert updated.hypotheses == [
        {
            "description": "pipeline stalled",
            "supporting_evidence": ["etl_agent"],
            "confidence_score": 0.8,
        }
    ]
    assert updated.evidence[0]["source"] == "etl_agent"
    assert updated.evidence[0]["finding"] == "job failed"
    system_notes = [e for e in updated.evidence if e["source"] == "system"]
    assert len(system_notes) == 1
    assert "no database progress was detected for 30 minutes" in system_notes[0][
        "finding"
    ].lower()

    state = updated.workflow_state
    assert state["current_node"] == "validation"
    assert state["retry_count"] == 2
    assert state["validation_pass_count"] == 3
    assert state["follow_up_query"] == "row counts"
    assert state["stale_reclaimed"] is True
    assert state["previous_status"] == "investigating"
    assert "stale_reclaimed_at" in state
    assert "30 minutes" in state["stale_reclaim_reason"]


def test_reaper_is_idempotent():
    investigation = create_investigation("idempotent reclaim")
    update_investigation(
        investigation.id, status=InvestigationStatus.INVESTIGATING
    )
    _backdate_updated_at(investigation.id, minutes_ago=60)

    reap_stale_investigations(stale_minutes=30)
    first = get_investigation(investigation.id)
    first_system = [e for e in first.evidence if e["source"] == "system"]
    assert first.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert len(first_system) == 1

    reap_stale_investigations(stale_minutes=30)
    second = get_investigation(investigation.id)
    second_system = [e for e in second.evidence if e["source"] == "system"]
    assert second.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert len(second_system) == 1
    assert second_system[0]["finding"] == first_system[0]["finding"]


def test_conditional_update_skips_investigation_that_became_active():
    investigation = create_investigation("race with live progress")
    update_investigation(
        investigation.id,
        status=InvestigationStatus.INVESTIGATING,
        workflow_state={"current_node": "sql_analysis", "retry_count": 0},
    )
    _backdate_updated_at(investigation.id, minutes_ago=60)

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    # Simulate progress after candidate detection: bump updated_at to now.
    _backdate_updated_at(investigation.id, minutes_ago=0)

    assert (
        reclaim_stale_investigation(
            investigation.id, cutoff=cutoff, stale_minutes=30
        )
        is False
    )
    updated = get_investigation(investigation.id)
    assert updated.status == InvestigationStatus.INVESTIGATING
    assert updated.workflow_state.get("stale_reclaimed") is not True


def test_configured_threshold_controls_which_rows_are_stale(monkeypatch):
    monkeypatch.setenv("STALE_INVESTIGATION_MINUTES", "10")
    investigation = create_investigation("threshold boundary")
    update_investigation(
        investigation.id, status=InvestigationStatus.INVESTIGATING
    )
    _backdate_updated_at(investigation.id, minutes_ago=15)

    reap_stale_investigations()
    assert get_investigation(investigation.id).status == (
        InvestigationStatus.NEEDS_HUMAN_REVIEW
    )


def test_recent_row_survives_stricter_configured_threshold(monkeypatch):
    monkeypatch.setenv("STALE_INVESTIGATION_MINUTES", "10")
    investigation = create_investigation("too fresh for threshold")
    update_investigation(
        investigation.id, status=InvestigationStatus.INVESTIGATING
    )
    _backdate_updated_at(investigation.id, minutes_ago=5)

    reap_stale_investigations()
    assert get_investigation(investigation.id).status == (
        InvestigationStatus.INVESTIGATING
    )


def test_reaper_failure_does_not_prevent_fastapi_startup():
    with patch(
        "app.api.main.reap_stale_investigations",
        side_effect=RuntimeError("simulated db outage"),
    ):
        with TestClient(app) as client:
            response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_lifespan_invokes_reaper():
    with patch("app.api.main.reap_stale_investigations", return_value=0) as reaper:
        with TestClient(app):
            pass
    reaper.assert_called_once_with()
