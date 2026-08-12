"""FastAPI entrypoint for the Data Lineage Investigator.

Step 10 adds the investigations HTTP API the React frontend calls:
create (kicks off a background graph run), fetch one, and list history.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
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
    reap_stale_investigations,
)
from app.db.models import Investigation
from app.investigation_runner import (
    mark_investigation_failed as _mark_investigation_failed,
    run_investigation_worker as _run_investigation_background,
)

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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """On startup, reclaim investigations orphaned by a dead worker.

    Failures here are logged and swallowed so a reaper/DB glitch cannot
    take the API offline.
    """
    logger.info("Startup stale-investigation reaper starting")
    try:
        reclaimed = reap_stale_investigations()
        logger.info(
            "Startup stale-investigation reaper finished reclaimed=%s",
            reclaimed,
        )
    except Exception:  # noqa: BLE001 - recovery must not block API startup
        logger.exception(
            "Startup stale-investigation reaper failed; API will still start"
        )
    yield


app = FastAPI(title="Data Lineage Investigator", lifespan=lifespan)

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
