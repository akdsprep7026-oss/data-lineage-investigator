"""Tests for the investigations table + CRUD helpers.

Creates a real investigation row in Postgres, appends a piece of
evidence via update_investigation(), and re-fetches it (via a brand new
session, simulating a process restart) to confirm the write actually
persisted rather than just living in an in-memory object.
"""

from __future__ import annotations

from app.db.investigations import (
    create_investigation,
    get_investigation,
    update_investigation,
)
from app.db.models import InvestigationStatus


def test_create_investigation_starts_pending_with_empty_lists():
    investigation = create_investigation(
        "Total revenue for region EU dropped 40% yesterday"
    )

    assert investigation.id is not None
    assert investigation.status == InvestigationStatus.PENDING
    assert investigation.evidence == []
    assert investigation.hypotheses == []
    assert investigation.final_root_cause is None
    assert investigation.created_at is not None
    assert investigation.updated_at is not None


def test_add_evidence_persists_and_is_visible_after_refetch():
    investigation = create_investigation(
        "Total revenue for region EU dropped 40% yesterday"
    )
    investigation_id = investigation.id

    updated = update_investigation(
        investigation_id,
        status=InvestigationStatus.INVESTIGATING,
        add_evidence={
            "source": "sql_models/01_stg_orders_cleaned.sql",
            "finding": "INNER JOIN against raw_customers drops orders "
            "from customers not yet present in raw_customers.",
            "confidence": 0.8,
        },
    )
    assert updated is not None
    assert updated.status == InvestigationStatus.INVESTIGATING
    assert len(updated.evidence) == 1

    # Re-fetch with a brand new session/connection -- simulates the
    # investigator process restarting and resuming by id, rather than
    # relying on any in-memory state.
    refetched = get_investigation(investigation_id)

    assert refetched is not None
    assert refetched.id == investigation_id
    assert refetched.status == InvestigationStatus.INVESTIGATING
    assert refetched.evidence == [
        {
            "source": "sql_models/01_stg_orders_cleaned.sql",
            "finding": "INNER JOIN against raw_customers drops orders "
            "from customers not yet present in raw_customers.",
            "confidence": 0.8,
        }
    ]
    # updated_at should have moved forward from created_at once we wrote
    # to the row.
    assert refetched.updated_at >= refetched.created_at


def test_add_hypothesis_and_final_root_cause():
    investigation = create_investigation("Dashboard shows no data for 2024-01-30")
    investigation_id = investigation.id

    update_investigation(
        investigation_id,
        add_hypothesis={
            "description": "build_fct_daily_revenue job has been failing",
            "supporting_evidence": ["pipeline_jobs.json last_run_status=failed"],
            "confidence_score": 0.9,
        },
    )
    resolved = update_investigation(
        investigation_id,
        status=InvestigationStatus.RESOLVED,
        final_root_cause="build_fct_daily_revenue failed for 2 days; "
        "fct_daily_revenue was never refreshed for those dates.",
    )

    assert resolved is not None
    assert len(resolved.hypotheses) == 1
    assert resolved.hypotheses[0]["confidence_score"] == 0.9
    assert resolved.status == InvestigationStatus.RESOLVED
    assert resolved.final_root_cause is not None

    refetched = get_investigation(investigation_id)
    assert refetched.status == InvestigationStatus.RESOLVED
    assert refetched.final_root_cause == resolved.final_root_cause


def test_get_investigation_returns_none_for_unknown_id():
    import uuid

    assert get_investigation(uuid.uuid4()) is None
