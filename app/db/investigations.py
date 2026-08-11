"""CRUD helpers for the `investigations` table.

These wrap plain SQLAlchemy ORM operations so agents can:
  - create_investigation(): start a new investigation (status='pending').
  - update_investigation(): append evidence/hypotheses, change status,
    and/or set the final root cause as the investigation progresses.
  - get_investigation(): re-fetch an investigation by id -- used both
    for normal reads and to resume an in-flight investigation after a
    process restart (all state lives in Postgres, not in memory).
  - reap_stale_investigations(): mark orphaned pending/investigating rows
    as needs_human_review after a configurable period with no DB progress
    (e.g. Render restart killed the in-process BackgroundTask).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import get_session
from app.db.models import Investigation, InvestigationStatus

logger = logging.getLogger(__name__)

DEFAULT_STALE_INVESTIGATION_MINUTES = 30

_NON_TERMINAL_STATUSES = (
    InvestigationStatus.PENDING,
    InvestigationStatus.INVESTIGATING,
)


def _coerce_uuid(investigation_id: uuid.UUID | str) -> uuid.UUID:
    return (
        investigation_id
        if isinstance(investigation_id, uuid.UUID)
        else uuid.UUID(str(investigation_id))
    )


def get_stale_investigation_minutes() -> int:
    """Minutes of no `updated_at` progress before a row is considered stale.

    Reads STALE_INVESTIGATION_MINUTES. Missing/blank →
    DEFAULT_STALE_INVESTIGATION_MINUTES. Invalid or non-positive values
    raise ValueError rather than silently reclaiming everything.
    """
    raw = (os.getenv("STALE_INVESTIGATION_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_STALE_INVESTIGATION_MINUTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            "STALE_INVESTIGATION_MINUTES must be a positive integer, "
            f"got {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError(
            "STALE_INVESTIGATION_MINUTES must be a positive integer, "
            f"got {value}"
        )
    return value


def _stale_reclaim_evidence(stale_minutes: int) -> dict[str, Any]:
    return {
        "source": "system",
        "finding": (
            "Investigation was automatically moved to human review because no "
            f"database progress was detected for {stale_minutes} minutes. The "
            "worker may have been interrupted by a process restart, "
            "deployment, crash, or timeout. Existing evidence and hypotheses "
            "were preserved."
        ),
        "confidence": 1.0,
    }


def reclaim_stale_investigation(
    investigation_id: uuid.UUID | str,
    *,
    cutoff: datetime,
    stale_minutes: int,
    session: Optional[Session] = None,
) -> bool:
    """Conditionally mark one stale investigation needs_human_review.

    Returns True only when a row was actually reclaimed. The UPDATE is
    gated on status still being pending/investigating *and* updated_at
    still older than `cutoff`, so a live worker that made progress after
    the candidate scan cannot be terminated.
    """
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware UTC")
    if stale_minutes <= 0:
        raise ValueError(f"stale_minutes must be positive, got {stale_minutes}")

    owns_session = session is None
    session = session or get_session()
    try:
        row = session.scalars(
            select(Investigation)
            .where(
                Investigation.id == _coerce_uuid(investigation_id),
                Investigation.status.in_(_NON_TERMINAL_STATUSES),
                Investigation.updated_at < cutoff,
            )
            .with_for_update()
        ).first()
        if row is None:
            # Release any open transaction/lock from the miss so a shared
            # session (or pooler) is not left idle-in-transaction.
            session.rollback()
            return False

        previous_status = row.status.value
        last_updated = row.updated_at
        prior_state = dict(row.workflow_state or {})

        # Idempotent: already reclaimed in a prior pass that somehow left
        # a non-terminal status -- do not duplicate the system note.
        if prior_state.get("stale_reclaimed"):
            row.status = InvestigationStatus.NEEDS_HUMAN_REVIEW
            session.commit()
            return True

        system_note = _stale_reclaim_evidence(stale_minutes)
        evidence = list(row.evidence or [])
        already = {
            (item.get("source"), item.get("finding")) for item in evidence
        }
        if (system_note["source"], system_note["finding"]) not in already:
            evidence.append(system_note)

        reclaimed_at = datetime.now(timezone.utc)
        merged_state = {
            **prior_state,
            "stale_reclaimed": True,
            "stale_reclaimed_at": reclaimed_at.isoformat(),
            "stale_reclaim_reason": (
                f"No database progress detected for {stale_minutes} minutes"
            ),
            "previous_status": previous_status,
        }

        logger.info(
            "Reclaiming stale investigation id=%s previous_status=%s "
            "last_updated_at=%s reason=%s",
            row.id,
            previous_status,
            last_updated.isoformat() if last_updated else None,
            merged_state["stale_reclaim_reason"],
        )

        row.status = InvestigationStatus.NEEDS_HUMAN_REVIEW
        row.evidence = evidence
        row.workflow_state = merged_state
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()


def reap_stale_investigations(
    *,
    stale_minutes: Optional[int] = None,
    now: Optional[datetime] = None,
    session: Optional[Session] = None,
) -> int:
    """Mark orphaned pending/investigating rows as needs_human_review.

    Staleness is based solely on `updated_at` (last DB progress), not
    `created_at`. Returns the number of rows successfully reclaimed.
    """
    minutes = (
        stale_minutes
        if stale_minutes is not None
        else get_stale_investigation_minutes()
    )
    if minutes <= 0:
        raise ValueError(f"stale_minutes must be positive, got {minutes}")

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware UTC")
    else:
        now = now.astimezone(timezone.utc)

    cutoff = now - timedelta(minutes=minutes)
    logger.info(
        "Stale-investigation reaper started threshold_minutes=%s cutoff=%s",
        minutes,
        cutoff.isoformat(),
    )

    owns_session = session is None
    session = session or get_session()
    try:
        candidate_ids = list(
            session.scalars(
                select(Investigation.id).where(
                    Investigation.status.in_(_NON_TERMINAL_STATUSES),
                    Investigation.updated_at < cutoff,
                )
            ).all()
        )
        logger.info(
            "Stale-investigation reaper candidates=%s", len(candidate_ids)
        )

        reclaimed = 0
        for investigation_id in candidate_ids:
            if reclaim_stale_investigation(
                investigation_id,
                cutoff=cutoff,
                stale_minutes=minutes,
                session=session,
            ):
                reclaimed += 1

        logger.info(
            "Stale-investigation reaper completed reclaimed=%s candidates=%s",
            reclaimed,
            len(candidate_ids),
        )
        return reclaimed
    finally:
        if owns_session:
            session.close()


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


def list_investigations(
    session: Optional[Session] = None,
) -> list[Investigation]:
    """Returns every investigation, newest first -- the History view."""
    owns_session = session is None
    session = session or get_session()
    try:
        return list(
            session.scalars(
                select(Investigation).order_by(Investigation.created_at.desc())
            ).all()
        )
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
    workflow_state: Optional[dict[str, Any]] = None,
    session: Optional[Session] = None,
) -> Optional[Investigation]:
    """Applies a partial update to an existing investigation:
      - add_evidence: appends one {"source", "finding", "confidence"}
        object to the evidence JSONB list.
      - add_hypothesis: appends one {"description", "supporting_evidence",
        "confidence_score"} object to the hypotheses JSONB list.
      - workflow_state: replaces the loop-control snapshot wholesale
        (unlike evidence/hypotheses, it's a current position, not an
        append-only log).
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
        if workflow_state is not None:
            investigation.workflow_state = dict(workflow_state)
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
