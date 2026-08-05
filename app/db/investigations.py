"""CRUD helpers for the `investigations` table.

These wrap plain SQLAlchemy ORM operations so agents can:
  - create_investigation(): start a new investigation (status='pending').
  - update_investigation(): append evidence/hypotheses, change status,
    and/or set the final root cause as the investigation progresses.
  - get_investigation(): re-fetch an investigation by id -- used both
    for normal reads and to resume an in-flight investigation after a
    process restart (all state lives in Postgres, not in memory).
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db.base import get_session
from app.db.models import Investigation, InvestigationStatus


def _coerce_uuid(investigation_id: uuid.UUID | str) -> uuid.UUID:
    return (
        investigation_id
        if isinstance(investigation_id, uuid.UUID)
        else uuid.UUID(str(investigation_id))
    )


def create_investigation(
    issue_description: str,
    session: Optional[Session] = None,
) -> Investigation:
    """Creates a new investigation in 'pending' status with empty
    evidence/hypotheses lists, and returns the persisted row (with its
    generated id, created_at, etc. populated)."""
    owns_session = session is None
    session = session or get_session()
    try:
        investigation = Investigation(
            issue_description=issue_description,
            status=InvestigationStatus.PENDING,
            evidence=[],
            hypotheses=[],
        )
        session.add(investigation)
        session.commit()
        session.refresh(investigation)
        return investigation
    finally:
        if owns_session:
            session.close()


def get_investigation(
    investigation_id: uuid.UUID | str,
    session: Optional[Session] = None,
) -> Optional[Investigation]:
    """Fetches an investigation by id, or None if it doesn't exist."""
    owns_session = session is None
    session = session or get_session()
    try:
        return session.get(Investigation, _coerce_uuid(investigation_id))
    finally:
        if owns_session:
            session.close()


def update_investigation(
    investigation_id: uuid.UUID | str,
    *,
    status: Optional[InvestigationStatus] = None,
    add_evidence: Optional[dict[str, Any]] = None,
    add_hypothesis: Optional[dict[str, Any]] = None,
    final_root_cause: Optional[str] = None,
    session: Optional[Session] = None,
) -> Optional[Investigation]:
    """Applies a partial update to an existing investigation:
      - add_evidence: appends one {"source", "finding", "confidence"}
        object to the evidence JSONB list.
      - add_hypothesis: appends one {"description", "supporting_evidence",
        "confidence_score"} object to the hypotheses JSONB list.
      - status / final_root_cause: set directly if provided.

    All arguments besides investigation_id are optional so callers can
    update just one thing at a time (e.g. only append a piece of
    evidence, without touching status). Returns the updated
    investigation, or None if no investigation with that id exists.
    """
    owns_session = session is None
    session = session or get_session()
    try:
        investigation = session.get(Investigation, _coerce_uuid(investigation_id))
        if investigation is None:
            return None

        if add_evidence is not None:
            # Reassign (rather than .append()) so SQLAlchemy detects the
            # change to this JSONB column and issues an UPDATE.
            investigation.evidence = [*investigation.evidence, add_evidence]
        if add_hypothesis is not None:
            investigation.hypotheses = [*investigation.hypotheses, add_hypothesis]
        if status is not None:
            investigation.status = status
        if final_root_cause is not None:
            investigation.final_root_cause = final_root_cause

        session.commit()
        session.refresh(investigation)
        return investigation
    finally:
        if owns_session:
            session.close()
