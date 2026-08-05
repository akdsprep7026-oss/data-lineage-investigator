"""Incident 2: stale pipeline (fct_daily_revenue not refreshed).

Simulates the build_fct_daily_revenue Airflow job failing for the last
two days of the window (timeouts connecting to the warehouse).
fct_daily_revenue is not refreshed for those two days, so the dashboard
goes stale/missing for the most recent data even though upstream data
(raw_orders, stg_orders_cleaned) is complete and up to date.

Apply: python -m app.sandbox_data.incidents.incident_02_stale_pipeline
   or: python -m app.sandbox_data.incidents.manage apply 2
Reset: python -m app.sandbox_data.incidents.manage reset
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import text

from app.sandbox_data.incidents import common
from app.sandbox_data.models import get_engine

STALE_DAYS = 2
WINDOW_END = date(2024, 1, 30)


def apply() -> None:
    common.reset_to_clean_baseline()
    engine = get_engine()

    stale_dates = [
        (WINDOW_END - timedelta(days=offset)).isoformat() for offset in range(STALE_DAYS)
    ]

    with engine.begin() as connection:
        for stale_date in stale_dates:
            connection.execute(
                text("DELETE FROM fct_daily_revenue WHERE date = :d"),
                {"d": stale_date},
            )

    jobs = json.loads(common.PIPELINE_JOBS_PATH.read_text(encoding="utf-8"))
    for job in jobs["jobs"]:
        if job["job_name"] == "build_fct_daily_revenue":
            job["last_run_status"] = "failed"
            job["last_run_duration_seconds"] = 300
            job["last_run_at"] = "2024-01-28T15:15:07Z"
            job["error_message"] = (
                "Connection to warehouse timed out after 300s. Job has not "
                f"completed successfully since 2024-01-28T15:15:07Z ({STALE_DAYS} "
                "days ago); fct_daily_revenue has not been refreshed since then."
            )
    common.PIPELINE_JOBS_PATH.write_text(
        json.dumps(jobs, indent=2) + "\n", encoding="utf-8"
    )

    print("Incident 2 (stale pipeline) applied.")
    print(f"  Removed fct_daily_revenue rows for: {', '.join(sorted(stale_dates))}")
    print("  Marked build_fct_daily_revenue as 'failed' in pipeline_jobs.json.")


if __name__ == "__main__":
    apply()
