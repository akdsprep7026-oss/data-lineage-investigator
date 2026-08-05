"""Incident 3: schema change (breaks the staging transformation).

Simulates a breaking upstream schema change: raw_orders.created_at is
renamed to order_created_at, but sql_models/01_stg_orders_cleaned.sql
still references the old column name (o.created_at). Every run of
build_stg_orders_cleaned now fails with a database error, and
stg_orders_cleaned / fct_daily_revenue are frozen at their last-good
(pre-incident) contents.

Apply: python -m app.sandbox_data.incidents.incident_03_schema_change
   or: python -m app.sandbox_data.incidents.manage apply 3
Reset: python -m app.sandbox_data.incidents.manage reset
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.sandbox_data import pipeline
from app.sandbox_data.incidents import common
from app.sandbox_data.models import get_engine


def apply() -> None:
    common.reset_to_clean_baseline()
    engine = get_engine()

    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE raw_orders RENAME COLUMN created_at TO order_created_at"
            )
        )

    error_message = None
    try:
        with engine.begin() as connection:
            pipeline.rebuild_stg_orders_cleaned(connection)
    except OperationalError as exc:
        # The failing transaction is rolled back automatically, so
        # stg_orders_cleaned / fct_daily_revenue keep their previous
        # (clean) contents -- exactly what a real failed dbt/Airflow run
        # would leave behind.
        error_message = str(exc.orig)

    jobs = json.loads(common.PIPELINE_JOBS_PATH.read_text(encoding="utf-8"))
    for job in jobs["jobs"]:
        if job["job_name"] == "build_stg_orders_cleaned":
            job["last_run_status"] = "failed"
            job["last_run_duration_seconds"] = 2
            job["last_run_at"] = "2024-01-31T06:00:05Z"
            job["error_message"] = error_message or "no such column: o.created_at"
    common.PIPELINE_JOBS_PATH.write_text(
        json.dumps(jobs, indent=2) + "\n", encoding="utf-8"
    )

    print("Incident 3 (schema change) applied.")
    print("  raw_orders.created_at renamed to raw_orders.order_created_at.")
    print(f"  build_stg_orders_cleaned now fails with: {error_message}")
    print(
        "  stg_orders_cleaned / fct_daily_revenue are frozen at their last "
        "successful (clean) state."
    )


if __name__ == "__main__":
    apply()
