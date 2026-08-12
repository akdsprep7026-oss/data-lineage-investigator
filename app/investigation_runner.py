"""Shared investigation worker used by FastAPI BackgroundTasks and Streamlit.

Keeps terminal-failure semantics in one place so Streamlit does not invent a
second state machine. Calls the existing `run_investigation` entry point only.
"""

from __future__ import annotations

import logging

from app.db.investigations import get_investigation, update_investigation
from app.db.models import InvestigationStatus
from app.graph.workflow import run_investigation

logger = logging.getLogger(__name__)


def mark_investigation_failed(
    investigation_id: str, exc: BaseException
) -> None:
    """Persist a terminal failure status without wiping prior agent work.

    Uses the existing update_investigation helper so evidence/hypotheses
    already written by graph nodes are preserved. workflow_state is merged
    (not blanked) because that helper replaces the JSONB object wholesale.
    """
    existing = get_investigation(investigation_id)
    prior_state = dict(existing.workflow_state or {}) if existing else {}
    error_type = type(exc).__name__
    error_message = str(exc) or error_type
    prior_state.update(
        {
            "current_node": "background_failure",
            "failed": True,
            "error_type": error_type,
            "error": error_message,
        }
    )
    update_investigation(
        investigation_id,
        status=InvestigationStatus.NEEDS_HUMAN_REVIEW,
        add_evidence={
            "source": "system",
            "finding": (
                "Investigation stopped unexpectedly before a final decision "
                f"could be recorded ({error_type}: {error_message}). "
                "Prior evidence and hypotheses were preserved for review."
            ),
            "confidence": 1.0,
        },
        workflow_state=prior_state,
    )


def run_investigation_worker(
    issue_description: str, investigation_id: str
) -> None:
    """Runs the LangGraph workflow for a previously created row.

    Isolated so a failure in the graph doesn't take down the host process
    (uvicorn worker or Streamlit). On success, human_review_node writes
    resolved / needs_human_review. On any unexpected exception, the row is
    forced to needs_human_review with a system failure note so it never
    stays stuck in investigating.
    """
    logger.info("Background investigation starting id=%s", investigation_id)
    try:
        final_state = run_investigation(
            issue_description, investigation_id=investigation_id
        )
    except Exception as exc:  # noqa: BLE001 - must not raise into host
        logger.exception(
            "Background investigation failed for id=%s", investigation_id
        )
        try:
            mark_investigation_failed(investigation_id, exc)
            logger.info(
                "Background investigation id=%s marked needs_human_review "
                "after failure (%s)",
                investigation_id,
                type(exc).__name__,
            )
        except Exception:  # noqa: BLE001 - last resort; never crash the worker
            logger.exception(
                "Failed to persist terminal failure status for id=%s",
                investigation_id,
            )
        return

    status = final_state.get("status")
    logger.info(
        "Background investigation finished id=%s status=%s "
        "retry_count=%s validation_pass_count=%s",
        investigation_id,
        status,
        final_state.get("retry_count", 0),
        final_state.get("validation_pass_count", 0),
    )

    # Belt-and-suspenders: if invoke returned without a terminal DB
    # status (should not happen after _ensure_terminal_status), force one.
    if status not in {
        InvestigationStatus.RESOLVED.value,
        InvestigationStatus.NEEDS_HUMAN_REVIEW.value,
    }:
        logger.error(
            "Background investigation id=%s returned non-terminal "
            "status=%s; forcing needs_human_review",
            investigation_id,
            status,
        )
        try:
            mark_investigation_failed(
                investigation_id,
                RuntimeError(
                    f"graph returned non-terminal status={status!r}"
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to force terminal status for id=%s", investigation_id
            )
        return

    persisted = get_investigation(investigation_id)
    if (
        persisted is not None
        and persisted.status == InvestigationStatus.INVESTIGATING
    ):
        logger.error(
            "Background investigation id=%s finished in-memory as %s but "
            "DB row still investigating; forcing needs_human_review",
            investigation_id,
            status,
        )
        try:
            mark_investigation_failed(
                investigation_id,
                RuntimeError(
                    "in-memory terminal status was not persisted to the "
                    "investigations row"
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to force terminal status for id=%s", investigation_id
            )
