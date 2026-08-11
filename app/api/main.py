"""FastAPI entrypoint for the Data Lineage Investigator.

Step 10 adds the investigations HTTP API the React frontend calls:
create (kicks off a background graph run), fetch one, and list history.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.api.schemas import (
    InvestigationCreate,
    InvestigationCreateResponse,
    InvestigationDetail,
    InvestigationSummary,
)
from app.db.investigations import (
    create_investigation,
    get_investigation,
    list_investigations,
    update_investigation,
)
from app.db.models import Investigation, InvestigationStatus
from app.graph.workflow import run_investigation

load_dotenv()

logger = logging.getLogger(__name__)

# Local Vite defaults when ALLOWED_ORIGINS is unset.
_DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def _cors_origins() -> list[str]:
    """Comma-separated ALLOWED_ORIGINS, or local Vite defaults if unset."""
    raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    if not raw:
        return list(_DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


app = FastAPI(title="Data Lineage Investigator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _summary(investigation: Investigation) -> InvestigationSummary:
    return InvestigationSummary(
        id=investigation.id,
        issue_description=investigation.issue_description,
        status=investigation.status.value,
        final_root_cause=investigation.final_root_cause,
        created_at=investigation.created_at,
        updated_at=investigation.updated_at,
    )


def _detail(investigation: Investigation) -> InvestigationDetail:
    return InvestigationDetail(
        id=investigation.id,
        issue_description=investigation.issue_description,
        status=investigation.status.value,
        evidence=list(investigation.evidence or []),
        hypotheses=list(investigation.hypotheses or []),
        final_root_cause=investigation.final_root_cause,
        created_at=investigation.created_at,
        updated_at=investigation.updated_at,
    )


def _mark_investigation_failed(
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


def _run_investigation_background(
    issue_description: str, investigation_id: str
) -> None:
    """Runs the LangGraph workflow for a previously created row.

    Isolated so a failure in the graph doesn't take down the API worker.
    On success, human_review_node writes resolved / needs_human_review.
    On any unexpected exception, the row is forced to needs_human_review
    with a system failure note so it never stays stuck in investigating.
    """
    logger.info(
        "Background investigation starting id=%s", investigation_id
    )
    try:
        final_state = run_investigation(
            issue_description, investigation_id=investigation_id
        )
    except Exception as exc:  # noqa: BLE001 - must not raise into uvicorn
        logger.exception(
            "Background investigation failed for id=%s", investigation_id
        )
        try:
            _mark_investigation_failed(investigation_id, exc)
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

    logger.info(
        "Background investigation finished id=%s status=%s",
        investigation_id,
        final_state.get("status"),
    )


@app.post("/investigations", response_model=InvestigationCreateResponse)
def post_investigation(
    body: InvestigationCreate,
    background_tasks: BackgroundTasks,
) -> InvestigationCreateResponse:
    """Creates a pending investigation and starts `run_investigation` in
    the background. Returns immediately with `{id, status}` so the
    frontend can navigate to the detail view and poll."""
    investigation = create_investigation(body.issue_description)
    background_tasks.add_task(
        _run_investigation_background,
        body.issue_description,
        str(investigation.id),
    )
    return InvestigationCreateResponse(
        id=investigation.id,
        status=investigation.status.value,
    )


@app.get("/investigations", response_model=list[InvestigationSummary])
def get_investigations() -> list[InvestigationSummary]:
    """History list: past investigations with status and root cause."""
    return [_summary(item) for item in list_investigations()]


@app.get("/investigations/{investigation_id}", response_model=InvestigationDetail)
def get_investigation_detail(investigation_id: UUID) -> InvestigationDetail:
    """Detail view (+ polling target while status is investigating)."""
    investigation = get_investigation(investigation_id)
    if investigation is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return _detail(investigation)
