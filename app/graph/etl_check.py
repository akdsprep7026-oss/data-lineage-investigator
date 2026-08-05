"""Direct checks of pipeline_jobs.json for failed, late, or otherwise
unhealthy runs, used by etl_agent_node.

Deliberately simple and deterministic -- no LLM involved -- mirroring
app/graph/data_quality.py's role for the data layer: data_quality_node
looks at what a table's *contents* say about a pipeline problem
(missing/duplicate/null rows), while this looks at what the job
*metadata* says directly, which is the more direct signal when a job
has simply stopped succeeding (or is running unusually slowly) rather
than corrupting the data it does produce.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, TypedDict

SANDBOX_DIR = Path(__file__).resolve().parents[1] / "sandbox_data"
PIPELINE_JOBS_PATH = SANDBOX_DIR / "pipeline_jobs.json"

# Every job in pipeline_jobs.json normally finishes in under a minute;
# comfortably above that is "late" even though it still reported
# success, which is worth surfacing as a softer signal.
LATE_RUN_THRESHOLD_SECONDS = 120


class JobHealthReport(TypedDict):
    job_name: str
    status: str  # "healthy" | "late" | "failed"
    last_run_status: str
    last_run_at: Optional[str]
    last_run_duration_seconds: Optional[int]
    upstream_tables: list[str]
    downstream_tables: list[str]
    error_message: Optional[str]


def load_pipeline_jobs(path: Path = PIPELINE_JOBS_PATH) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("jobs", [])


def _touches_any(job: dict, tables: set[str]) -> bool:
    if not tables:
        return True
    touched = set(job.get("upstream_tables") or []) | set(job.get("downstream_tables") or [])
    return bool(touched & tables)


def _classify(job: dict) -> str:
    if job.get("last_run_status") != "success":
        return "failed"
    duration = job.get("last_run_duration_seconds")
    if duration is not None and duration > LATE_RUN_THRESHOLD_SECONDS:
        return "late"
    return "healthy"


def check_jobs_for_tables(
    tables: list[str], jobs: Optional[list[dict]] = None
) -> list[JobHealthReport]:
    """Health-checks every pipeline job that reads from or writes to any
    of `tables` -- flagging one whose last run failed outright, or one
    that succeeded but took unusually long, as distinct from a job
    that's simply healthy.

    If `tables` is empty (lineage_agent_node didn't tag any), every job
    is checked instead of none, since pipeline_jobs.json is small
    enough that this costs nothing and an empty result here would look
    identical to "nothing to report" rather than "nothing was relevant."
    """
    jobs = jobs if jobs is not None else load_pipeline_jobs()
    table_set = set(tables)
    reports: list[JobHealthReport] = []
    for job in jobs:
        if not _touches_any(job, table_set):
            continue
        reports.append(
            JobHealthReport(
                job_name=job["job_name"],
                status=_classify(job),
                last_run_status=job.get("last_run_status", "unknown"),
                last_run_at=job.get("last_run_at"),
                last_run_duration_seconds=job.get("last_run_duration_seconds"),
                upstream_tables=list(job.get("upstream_tables") or []),
                downstream_tables=list(job.get("downstream_tables") or []),
                error_message=job.get("error_message"),
            )
        )
    return reports
