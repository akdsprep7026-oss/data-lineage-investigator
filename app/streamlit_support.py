"""Pure helpers for the Streamlit adapter (no Streamlit imports).

Keeps duplicate-thread guards, reaper-once init, display shaping, and
lightweight runtime readiness checks testable without a Streamlit browser.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict
from collections.abc import MutableMapping
from typing import Any, Iterable, Optional

from app.db.investigations import reap_stale_investigations
from app.db.models import InvestigationStatus
from app.investigation_runner import run_investigation_worker
from app.retrieval.ingest import CHROMA_PERSIST_DIR, COLLECTION_NAME, get_chroma_client

logger = logging.getLogger(__name__)

PLACEHOLDER_PREFIX = "your-"
RETRIEVAL_INGEST_COMMAND = "python -m app.retrieval.ingest"
SANDBOX_SEED_COMMAND = "python -m app.sandbox_data.seed"
# Opt-in Streamlit sidebar panel for applying sandbox incidents on a live
# process (local or Community Cloud). Off unless ENABLE_SANDBOX_DEBUG=true.
SANDBOX_DEBUG_CLEAN = "clean baseline"
SANDBOX_DEBUG_OPTIONS = (
    (SANDBOX_DEBUG_CLEAN, "clean baseline"),
    ("1", "1 - join bug"),
    ("2", "2 - stale pipeline"),
    ("3", "3 - schema change"),
    ("4", "4 - duplicate rows"),
)
REQUIRED_WAREHOUSE_TABLES = frozenset(
    {
        "raw_customers",
        "raw_orders",
        "stg_orders_cleaned",
        "fct_daily_revenue",
    }
)

IN_PROGRESS_STATUSES = frozenset(
    {
        InvestigationStatus.PENDING.value,
        InvestigationStatus.INVESTIGATING.value,
    }
)

POLL_INTERVAL_SECONDS = 2.0
PENDING_SIDEBAR_NAV_KEY = "_pending_sidebar_nav"
SIDEBAR_NAV_KEY = "sidebar_nav"
PREV_SIDEBAR_NAV_KEY = "_prev_sidebar_nav"

_reaper_lock = threading.Lock()
_reaper_ran = False

_bootstrap_lock = threading.Lock()
_bootstrap_ran = False
_bootstrap_result: Optional[dict[str, str]] = None


def queue_sidebar_nav(
    session_state: MutableMapping[str, Any],
    nav: str,
    *,
    pending_key: str = PENDING_SIDEBAR_NAV_KEY,
) -> None:
    """Queue a sidebar radio value for the *next* script run.

    Streamlit forbids assigning ``session_state[widget_key]`` after the
    widget that owns ``widget_key`` has already been instantiated in the
    current run. Button handlers must queue navigation, then ``st.rerun()``.
    """
    session_state[pending_key] = nav


def apply_pending_sidebar_nav(
    session_state: MutableMapping[str, Any],
    *,
    pending_key: str = PENDING_SIDEBAR_NAV_KEY,
    nav_key: str = SIDEBAR_NAV_KEY,
    prev_key: str = PREV_SIDEBAR_NAV_KEY,
) -> Optional[str]:
    """Apply queued sidebar navigation *before* ``st.radio`` is created.

    Returns the applied nav label, or None when nothing was pending.
    """
    pending = session_state.pop(pending_key, None)
    if pending is None:
        return None
    session_state[nav_key] = pending
    session_state[prev_key] = pending
    return str(pending)


def ensure_startup_reaper_once() -> None:
    """Call reap_stale_investigations at most once per process.

    Mirrors FastAPI lifespan: failures are logged and swallowed so a
    reaper/DB glitch cannot take the Streamlit app offline.
    """
    global _reaper_ran
    with _reaper_lock:
        if _reaper_ran:
            return
        _reaper_ran = True
        logger.info("Streamlit startup stale-investigation reaper starting")
        try:
            reclaimed = reap_stale_investigations()
            logger.info(
                "Streamlit startup stale-investigation reaper finished "
                "reclaimed=%s",
                reclaimed,
            )
        except Exception:  # noqa: BLE001 - recovery must not block UI
            logger.exception(
                "Streamlit startup stale-investigation reaper failed; "
                "UI will still start"
            )


def reset_startup_reaper_for_tests() -> None:
    """Test-only: allow ensure_startup_reaper_once to run again."""
    global _reaper_ran
    with _reaper_lock:
        _reaper_ran = False


def reset_runtime_bootstrap_for_tests() -> None:
    """Test-only: allow ensure_runtime_assets_once to run again."""
    global _bootstrap_ran, _bootstrap_result
    with _bootstrap_lock:
        _bootstrap_ran = False
        _bootstrap_result = None


def is_in_progress(status: Optional[str]) -> bool:
    return (status or "") in IN_PROGRESS_STATUSES


def should_start_worker(
    investigation_id: str, started_ids: Iterable[str]
) -> bool:
    """True only when this investigation has not already been dispatched."""
    return investigation_id not in set(started_ids)


def start_investigation_thread(
    issue_description: str, investigation_id: str
) -> threading.Thread:
    """Start a daemon thread that runs the shared investigation worker."""
    thread = threading.Thread(
        target=run_investigation_worker,
        args=(issue_description, investigation_id),
        name=f"investigation-{investigation_id}",
        daemon=True,
    )
    thread.start()
    return thread


def rank_hypotheses(hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Highest confidence_score first; skips non-dicts / bad scores."""

    def _score(item: dict[str, Any]) -> float:
        try:
            return float(item.get("confidence_score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    usable = [item for item in (hypotheses or []) if isinstance(item, dict)]
    return sorted(usable, key=_score, reverse=True)


def group_evidence_by_source(
    evidence: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group evidence by source, preserving first-seen source order."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "unknown")
        if source not in grouped:
            order.append(source)
        grouped[source].append(item)
    return [(source, grouped[source]) for source in order]


def workflow_progress_message(workflow_state: Optional[dict[str, Any]]) -> str:
    """Short progress line from persisted workflow_state, if present."""
    state = workflow_state or {}
    if not isinstance(state, dict):
        return "Investigation in progress…"
    node = state.get("current_node")
    if state.get("failed"):
        error = state.get("error") or state.get("error_type") or "unknown error"
        return f"Investigation failed during background run ({error})."
    if node:
        retry = state.get("retry_count")
        if retry is not None:
            return f"Running node `{node}` (retry_count={retry})."
        return f"Running node `{node}`."
    return "Investigation in progress…"


def format_confidence_pct(value: Any) -> str:
    """Present a 0–1 confidence as a percentage string without mutating storage."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.0
    pct = max(0, min(100, int(round(score * 100))))
    return f"{pct}%"


def format_timestamp(value: Any) -> str:
    """Human-readable timestamp; passes through None/empty safely."""
    if value is None:
        return "—"
    text = str(value).strip()
    return text or "—"


def truncate_text(text: Any, max_chars: int = 120) -> str:
    raw = str(text or "").strip()
    if len(raw) <= max_chars:
        return raw
    return f"{raw[:max_chars].rstrip()}…"


def root_cause_placeholder(status: Optional[str]) -> str:
    """Status-aware message when final_root_cause is absent (no fabrication)."""
    if is_in_progress(status):
        return "Investigation is still in progress."
    if status == InvestigationStatus.NEEDS_HUMAN_REVIEW.value:
        return "The investigation requires human review."
    return "No final root cause recorded."


def should_poll_status(status: Optional[str]) -> bool:
    """True while the React UI would keep polling (~2s)."""
    return is_in_progress(status)


def useful_workflow_state(
    workflow_state: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return a compact workflow snapshot worth showing, else None."""
    if not isinstance(workflow_state, dict) or not workflow_state:
        return None
    keys = (
        "current_node",
        "retry_count",
        "validation_pass_count",
        "agents_to_run",
        "agents_completed",
        "failed",
        "error_type",
        "error",
    )
    compact = {
        key: workflow_state[key]
        for key in keys
        if key in workflow_state and workflow_state[key] not in (None, [], {})
    }
    return compact or None


def investigation_row_to_snapshot(row: Any) -> dict[str, Any]:
    """Map an Investigation ORM row to plain dicts for UI rendering."""
    status = row.status.value if hasattr(row.status, "value") else str(row.status)
    workflow = row.workflow_state or {}
    return {
        "id": str(row.id),
        "issue_description": row.issue_description,
        "status": status,
        "evidence": list(row.evidence or []),
        "hypotheses": list(row.hypotheses or []),
        "workflow_state": dict(workflow) if isinstance(workflow, dict) else {},
        "final_root_cause": row.final_root_cause,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def history_item_to_summary(row: Any) -> dict[str, Any]:
    """Compact history row for list rendering (newest-first from service)."""
    status = row.status.value if hasattr(row.status, "value") else str(row.status)
    return {
        "id": str(row.id),
        "issue_description": row.issue_description,
        "status": status,
        "final_root_cause": row.final_root_cause,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _env_configured(name: str) -> bool:
    """True when an env var is set and is not an .env.example placeholder."""
    value = (os.getenv(name) or "").strip()
    if not value:
        return False
    return not value.lower().startswith(PLACEHOLDER_PREFIX)


def is_retrieval_ready() -> bool:
    """True when the persisted Chroma collection exists and has documents.

    Reuses the existing Chroma client path. Does not create collections,
    run ingest, or fabricate index data. Safe to call on Streamlit reruns.
    """
    if not CHROMA_PERSIST_DIR.exists():
        return False
    try:
        client = get_chroma_client()
        collection = client.get_collection(COLLECTION_NAME)
        return int(collection.count()) > 0
    except Exception:  # noqa: BLE001 - readiness must never crash the UI
        logger.exception("Retrieval readiness check failed")
        return False


def retrieval_status_label() -> str:
    return "Ready" if is_retrieval_ready() else "Not initialized"


def runtime_config_status() -> dict[str, str]:
    """Presence-only runtime summary for Streamlit (never includes secrets)."""
    from app.graph.llm import resolve_provider
    from app.graph.tracing import tracing_enabled
    from app.retrieval.embeddings import resolve_embedding_provider

    google = _env_configured("GOOGLE_API_KEY")
    groq = _env_configured("GROQ_API_KEY")
    database_url = _env_configured("DATABASE_URL")
    provider = resolve_provider()
    if provider is None:
        llm_mode = "heuristic fallback"
    else:
        llm_mode = provider

    return {
        "gemini_api_key": "configured" if google else "not configured",
        "groq_api_key": "configured" if groq else "not configured",
        "llm_mode": llm_mode,
        "embedding_provider": resolve_embedding_provider(),
        "langfuse": "configured" if tracing_enabled() else "not configured",
        "database": "DATABASE_URL" if database_url else "local embedded",
        "retrieval": "ready" if is_retrieval_ready() else "not initialized",
        "sandbox_warehouse": (
            "ready" if is_sandbox_warehouse_ready() else "not initialized"
        ),
        "streamlit_cloud": (
            "detected" if is_streamlit_cloud_runtime() else "local/other"
        ),
    }


def is_streamlit_cloud_runtime() -> bool:
    """Best-effort Cloud detection; prefer explicit STREAMLIT_CLOUD_DEPLOY=true.

    Local development remains unchanged when the flag is unset and the
    process is not running as Community Cloud's conventional appuser.
    """
    flag = (os.getenv("STREAMLIT_CLOUD_DEPLOY") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    user = (os.getenv("USER") or os.getenv("USERNAME") or "").strip()
    if user == "appuser":
        return True
    home = (os.getenv("HOME") or "").rstrip("/")
    if home == "/home/appuser":
        return True
    return False


def cloud_database_url_error() -> Optional[str]:
    """Error text when Cloud deploy lacks a real DATABASE_URL; else None.

    Local Streamlit (no Cloud indicators) may still use embedded pgserver.
    Cloud must not silently fall back to embedded Postgres.
    """
    if not is_streamlit_cloud_runtime():
        return None
    if _env_configured("DATABASE_URL"):
        return None
    return (
        "Streamlit Community Cloud requires DATABASE_URL pointing at an "
        "external PostgreSQL database. Embedded pgserver is for local "
        "development only and is not durable on Community Cloud. Set "
        "DATABASE_URL (and STREAMLIT_CLOUD_DEPLOY=true) in Streamlit Secrets."
    )


def is_sandbox_debug_enabled() -> bool:
    """True when ENABLE_SANDBOX_DEBUG is set (Streamlit Secrets / env).

    Default is off so production Cloud UIs stay free of incident controls
    unless explicitly enabled.
    """
    flag = (os.getenv("ENABLE_SANDBOX_DEBUG") or "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def sandbox_warehouse_row_counts() -> dict[str, int]:
    """Row counts for the sandbox SQLite warehouse in this process."""
    from sqlalchemy import text

    from app.sandbox_data.models import get_engine

    engine = get_engine()
    tables = (
        "raw_customers",
        "raw_orders",
        "stg_orders_cleaned",
        "fct_daily_revenue",
    )
    with engine.connect() as connection:
        return {
            table: int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
            for table in tables
        }


def apply_sandbox_debug_selection(selection_key: str) -> dict[str, Any]:
    """Apply clean baseline or incident 1–4, then re-ingest Chroma.

    Operates on this process's ``warehouse.db`` / SQL models / ``chroma_db``
    paths (the same files agents and validators read). Does not touch
    investigations Postgres (``DATABASE_URL``).
    """
    from app.retrieval.ingest import ingest
    from app.sandbox_data.incidents import common
    from app.sandbox_data.incidents.manage import INCIDENTS

    key = (selection_key or "").strip()
    if key == SANDBOX_DEBUG_CLEAN or key.lower() == "clean baseline":
        common.reset_to_clean_baseline()
        action = "Reset to clean baseline."
    elif key in INCIDENTS:
        INCIDENTS[key].apply()
        action = f"Applied incident {key}."
    else:
        return {
            "ok": False,
            "message": f"Unknown sandbox selection: {selection_key!r}",
            "counts": {},
            "docs_ingested": 0,
        }

    docs = ingest(reset=True)
    counts = sandbox_warehouse_row_counts()
    return {
        "ok": True,
        "message": (
            f"{action} Re-ingested {docs} retrieval document(s) so Chroma "
            "matches this process's sandbox files."
        ),
        "counts": counts,
        "docs_ingested": docs,
    }


def is_sandbox_warehouse_ready() -> bool:
    """True when sandbox SQLite exists and has the expected warehouse tables."""
    from sqlalchemy import inspect

    from app.sandbox_data.models import DEFAULT_SQLITE_PATH, get_engine

    if not DEFAULT_SQLITE_PATH.exists() or DEFAULT_SQLITE_PATH.stat().st_size == 0:
        return False
    try:
        tables = set(inspect(get_engine()).get_table_names())
        return REQUIRED_WAREHOUSE_TABLES.issubset(tables)
    except Exception:  # noqa: BLE001 - readiness must never crash the UI
        logger.exception("Sandbox warehouse readiness check failed")
        return False


def ensure_sandbox_warehouse() -> str:
    """Seed warehouse.db only when missing/uninitialized. Never on every rerun.

    Uses existing `app.sandbox_data.seed.seed`. That function rebuilds tables,
    so it is only invoked when the warehouse is not already ready.
    """
    if is_sandbox_warehouse_ready():
        return "ready"
    from app.sandbox_data.seed import seed

    logger.info("Sandbox warehouse missing/uninitialized; running seed once")
    seed()
    if not is_sandbox_warehouse_ready():
        raise RuntimeError(
            "Sandbox warehouse seed completed but tables are still missing. "
            f"Try `{SANDBOX_SEED_COMMAND}` locally and inspect warehouse.db."
        )
    return "seeded"


def ensure_retrieval_index() -> str:
    """Ingest Chroma only when the index is not ready. Never on every rerun.

    When the index is absent, calls existing `ingest(reset=True)` once so a
    fresh Community Cloud filesystem can build `sandbox_data`. When ready,
    skips entirely (no reset).
    """
    if is_retrieval_ready():
        return "ready"
    from app.retrieval.ingest import ingest

    logger.info("Retrieval index missing/uninitialized; running ingest once")
    count = ingest(reset=True)
    if not is_retrieval_ready():
        raise RuntimeError(
            "Retrieval ingest completed but Chroma is still not ready. "
            f"Try `{RETRIEVAL_INGEST_COMMAND}` with EMBEDDING_PROVIDER=onnx."
        )
    return f"ingested:{count}"


def ensure_runtime_assets_once() -> dict[str, str]:
    """Once per process: ensure sandbox warehouse + Chroma when missing.

    Safe across Streamlit reruns. Does not reseed/reingest when already ready.
    """
    global _bootstrap_ran, _bootstrap_result
    with _bootstrap_lock:
        if _bootstrap_ran and _bootstrap_result is not None:
            return dict(_bootstrap_result)

        result: dict[str, str] = {
            "warehouse": "error",
            "retrieval": "error",
        }
        try:
            result["warehouse"] = ensure_sandbox_warehouse()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sandbox warehouse bootstrap failed")
            result["warehouse"] = f"error:{type(exc).__name__}"
            result["warehouse_error"] = str(exc) or type(exc).__name__

        try:
            result["retrieval"] = ensure_retrieval_index()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Retrieval bootstrap failed")
            result["retrieval"] = f"error:{type(exc).__name__}"
            result["retrieval_error"] = str(exc) or type(exc).__name__

        _bootstrap_result = dict(result)
        _bootstrap_ran = True
        return dict(result)


def ensure_streamlit_startup_once() -> dict[str, str]:
    """Ordered Streamlit process startup (once): reaper → warehouse → Chroma."""
    ensure_startup_reaper_once()
    return ensure_runtime_assets_once()
