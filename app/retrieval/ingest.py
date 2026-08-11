"""Ingests app/sandbox_data content into a local, persistent Chroma
vector store so an agent can semantically search it via
app.retrieval.retriever.retrieve().

Three kinds of documents are indexed, each tagged with a "type" in its
metadata so retrieval can optionally be filtered:

  - sql_model       one chunk per SQL statement in each sql_models/*.sql
                     file (in practice one chunk per file today, since
                     each model is a single statement -- see
                     _split_sql_statements below for the multi-statement
                     case), metadata: {type, table_name, file_path}.
  - pipeline_job     one document per entry in pipeline_jobs.json,
                     metadata: {type, job_name}.
  - dashboard_widget one document per widget in dashboard_config.json,
                     metadata: {type, widget_name, source_table}.

Run directly to (re)build the index from the current sandbox_data state:

    python -m app.retrieval.ingest
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import chromadb

from app.retrieval.embeddings import (
    get_embedding_function,
    provider_from_chroma_name,
    resolve_embedding_provider,
)

RETRIEVAL_DIR = Path(__file__).resolve().parent
APP_DIR = RETRIEVAL_DIR.parent
PROJECT_ROOT = APP_DIR.parent
SANDBOX_DATA_DIR = APP_DIR / "sandbox_data"
SQL_MODELS_DIR = SANDBOX_DATA_DIR / "sql_models"
PIPELINE_JOBS_PATH = SANDBOX_DATA_DIR / "pipeline_jobs.json"
DASHBOARD_CONFIG_PATH = SANDBOX_DATA_DIR / "dashboard_config.json"

# Named "chroma_db" (not e.g. "chroma_store") to match the existing
# top-level .gitignore entry for chroma_db/.
CHROMA_PERSIST_DIR = RETRIEVAL_DIR / "chroma_db"
COLLECTION_NAME = "sandbox_data"

_TARGET_RE = re.compile(r"--\s*Target:\s*(\S+)", re.IGNORECASE)
_SQL_COMMENT_RE = re.compile(r"--.*")

Document = tuple[str, dict[str, Any], str]  # (text, metadata, id)


def _table_name_from_sql(content: str, file_path: Path) -> str:
    """Prefers the "-- Target: <name>" doc-comment (accurate even when a
    model isn't materialized 1:1 with its filename, e.g. incident 3's
    schema-change target or the rolling-avg model's mart_* target) and
    falls back to the filename if that comment is missing."""
    match = _TARGET_RE.search(content)
    if match:
        return match.group(1)
    return file_path.stem


def _split_sql_statements(sql_text: str) -> list[str]:
    """Splits SQL text into whole, top-level statements by only cutting
    at the semicolons that terminate a statement, so a chunk never cuts
    a query in half. Any comment-only text preceding the first real
    statement (e.g. our model header doc-comments) is kept attached to
    that statement rather than becoming its own empty chunk.

    This is a naive split (it doesn't account for semicolons inside
    string literals), which is fine for our sandbox SQL models. Today
    every model file is a single statement, so this returns exactly one
    chunk per file; it's written generically so a future multi-statement
    model file still gets chunked correctly instead of by raw size.
    """
    raw_parts = sql_text.split(";")
    statements: list[str] = []
    pending_prefix = ""
    for i, part in enumerate(raw_parts):
        piece = part if i == len(raw_parts) - 1 else part + ";"
        if _SQL_COMMENT_RE.sub("", piece).strip():
            statements.append((pending_prefix + piece).strip())
            pending_prefix = ""
        else:
            pending_prefix += piece
    if pending_prefix.strip():
        if statements:
            statements[-1] += "\n" + pending_prefix.strip()
        else:
            statements.append(pending_prefix.strip())
    return statements


def _load_sql_model_documents() -> list[Document]:
    documents: list[Document] = []
    for file_path in sorted(SQL_MODELS_DIR.glob("*.sql")):
        content = file_path.read_text(encoding="utf-8")
        table_name = _table_name_from_sql(content, file_path)
        relative_path = str(file_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        for idx, statement in enumerate(_split_sql_statements(content)):
            metadata = {
                "type": "sql_model",
                "table_name": table_name,
                "file_path": relative_path,
                "chunk_index": idx,
            }
            documents.append((statement, metadata, f"sql_model::{file_path.stem}::{idx}"))
    return documents


def _load_pipeline_job_documents() -> list[Document]:
    documents: list[Document] = []
    if not PIPELINE_JOBS_PATH.exists():
        return documents
    data = json.loads(PIPELINE_JOBS_PATH.read_text(encoding="utf-8"))
    for job in data.get("jobs", []):
        text = (
            f"Pipeline job: {job['job_name']}\n"
            f"Schedule (cron): {job['schedule']}\n"
            f"SQL model: {job['sql_model']}\n"
            f"Upstream tables: {', '.join(job.get('upstream_tables', []))}\n"
            f"Downstream tables: {', '.join(job.get('downstream_tables', []))}\n"
            f"Last run status: {job['last_run_status']} "
            f"(took {job['last_run_duration_seconds']}s, "
            f"last ran at {job['last_run_at']})"
        )
        metadata = {"type": "pipeline_job", "job_name": job["job_name"]}
        documents.append((text, metadata, f"pipeline_job::{job['job_name']}"))
    return documents


def _load_dashboard_widget_documents() -> list[Document]:
    documents: list[Document] = []
    if not DASHBOARD_CONFIG_PATH.exists():
        return documents
    data = json.loads(DASHBOARD_CONFIG_PATH.read_text(encoding="utf-8"))
    dashboard_name = data.get("dashboard_name", "")
    for widget in data.get("widgets", []):
        metric_names = ", ".join(m.get("name", "") for m in widget.get("metrics", []))
        text = (
            f"Dashboard: {dashboard_name}\n"
            f"Widget: {widget['widget_name']} ({widget.get('chart_type', 'unknown')} chart)\n"
            f"Source table: {widget.get('source_table', '')}\n"
            f"Dimensions: {', '.join(widget.get('dimensions', []))}\n"
            f"Metrics: {metric_names}\n"
            f"Filters: {json.dumps(widget.get('filters', {}))}"
        )
        metadata = {
            "type": "dashboard_widget",
            "widget_name": widget["widget_name"],
            "source_table": widget.get("source_table", ""),
        }
        documents.append((text, metadata, f"dashboard_widget::{widget['widget_name']}"))
    return documents


def get_chroma_client() -> chromadb.ClientAPI:
    CHROMA_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(CHROMA_PERSIST_DIR))


def _persisted_embedding_provider(client: chromadb.ClientAPI) -> str | None:
    """Provider name recorded on an existing collection, if any."""
    try:
        collection = client.get_collection(COLLECTION_NAME)
    except Exception:
        return None
    config = getattr(collection, "configuration_json", None) or {}
    ef = config.get("embedding_function") or {}
    return provider_from_chroma_name(ef.get("name"))


def get_collection(
    client: chromadb.ClientAPI | None = None,
    *,
    create_if_missing: bool = True,
):
    """Opens the sandbox_data collection with a compatible embedding fn.

    If the collection already exists, Chroma's persisted embedding
    function is reused (via get_collection with no override) so query
    never conflicts with a previously ingested index. New collections
    are created with the currently configured provider from
    app.retrieval.embeddings.
    """
    client = client or get_chroma_client()
    try:
        return client.get_collection(COLLECTION_NAME)
    except Exception:
        if not create_if_missing:
            raise
    return client.get_or_create_collection(
        COLLECTION_NAME,
        embedding_function=get_embedding_function(resolve_embedding_provider()),
    )


def ingest(reset: bool = True) -> int:
    """Builds the sandbox_data Chroma collection from the files on disk.
    Returns the number of documents ingested.

    If reset=True (the default), any existing collection is dropped
    first so re-running always reflects the current sandbox state --
    important since app/sandbox_data/incidents/*.py can rewrite the SQL
    model and pipeline_jobs.json files at any time. The rebuilt
    collection uses the embedding provider selected by
    EMBEDDING_PROVIDER / GOOGLE_API_KEY (see embeddings.py).
    """
    client = get_chroma_client()
    provider = resolve_embedding_provider()
    if reset:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
    else:
        existing = _persisted_embedding_provider(client)
        if existing is not None and existing != provider:
            raise RuntimeError(
                f"Chroma collection '{COLLECTION_NAME}' was built with "
                f"embedding provider {existing!r}, but the process is "
                f"configured for {provider!r}. Re-run ingest with "
                f"reset=True (the default for `python -m app.retrieval.ingest`) "
                f"or set EMBEDDING_PROVIDER={existing}."
            )

    collection = client.get_or_create_collection(
        COLLECTION_NAME,
        embedding_function=get_embedding_function(provider),
    )

    all_documents = (
        _load_sql_model_documents()
        + _load_pipeline_job_documents()
        + _load_dashboard_widget_documents()
    )
    if not all_documents:
        return 0

    texts, metadatas, ids = zip(*all_documents)
    collection.upsert(documents=list(texts), metadatas=list(metadatas), ids=list(ids))
    return len(all_documents)


if __name__ == "__main__":
    ingested_count = ingest()
    print(
        f"Ingested {ingested_count} documents into Chroma collection "
        f"'{COLLECTION_NAME}' at {CHROMA_PERSIST_DIR}"
    )
