"""FastAPI entrypoint for the Data Lineage Investigator.

Step 10 adds the investigations HTTP API the React frontend calls:
create (kicks off a background graph run), fetch one, and list history.
"""

from __future__ import annotations

import logging
from uuid import UUID

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
)
from app.db.models import Investigation
from app.graph.workflow import run_investigation

logger = logging.getLogger(__name__)

app = FastAPI(title="Data Lineage Investigator")

# Vite defaults to http://localhost:5173; allow local frontend origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
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


def _run_investigation_background(
    issue_description: str, investigation_id: str
) -> None:
    """Runs the LangGraph workflow for a previously created row.

    Isolated so a failure in the graph doesn't take down the API worker;
    the investigation row keeps whatever status/evidence it had when the
    failure happened (typically still `investigating` if the crash was
    early -- the UI will stop auto-refreshing once a human notices).
    """
    try:
        run_investigation(issue_description, investigation_id=investigation_id)
    except Exception:  # noqa: BLE001 - background task must not raise into uvicorn
        logger.exception(
            "Background investigation failed for id=%s", investigation_id
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
