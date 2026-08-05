"""Shared reset logic for incident scenarios.

Every incident script's apply() calls reset_to_clean_baseline() first, so
exactly one incident is ever active against the sandbox warehouse at a
time. Resetting:

  1. Restores sql_models/01_stg_orders_cleaned.sql and pipeline_jobs.json
     to their known-clean baseline content (the only two on-disk files
     any incident is allowed to mutate).
  2. Re-runs the seed script, which drops and recreates every table and
     regenerates deterministic (random.seed-pinned) raw + derived data.
     This alone undoes any raw-data mutation an incident made (injected
     orphan orders, injected duplicate orders, renamed columns, deleted
     fct rows, etc.) since the tables are rebuilt from scratch.
"""

from __future__ import annotations

import json
from pathlib import Path

SANDBOX_DIR = Path(__file__).resolve().parents[1]
SQL_MODELS_DIR = SANDBOX_DIR / "sql_models"
STG_ORDERS_CLEANED_SQL_PATH = SQL_MODELS_DIR / "01_stg_orders_cleaned.sql"
PIPELINE_JOBS_PATH = SANDBOX_DIR / "pipeline_jobs.json"

# Must stay byte-for-byte in sync with the clean file on disk -- this is
# what incident scripts that mutate the SQL model (join bug) restore.
CLEAN_STG_ORDERS_CLEANED_SQL = """-- stg_orders_cleaned.sql
--
-- Source(s): raw_orders, raw_customers
-- Target:    stg_orders_cleaned
--
-- Cleans raw_orders by:
--   1. Joining in each customer's region from raw_customers. A LEFT JOIN
--      is used (not INNER) so that orders from brand-new customers whose
--      record hasn't yet landed in raw_customers are still kept, with
--      region falling back to 'UNKNOWN', instead of being silently
--      dropped and undercounting revenue.
--   2. Filtering out cancelled orders and invalid (non-positive) amounts.
--   3. De-duplicating late-arriving duplicate raw events: raw_orders is an
--      append-only landing table, so the same order_id can show up more
--      than once (e.g. a status update re-emitted by the source system).
--      We keep only the most recently seen row per order_id using a
--      window function.

WITH ranked_orders AS (
    SELECT
        o.order_id,
        o.customer_id,
        COALESCE(c.region, 'UNKNOWN') AS region,
        o.amount,
        o.status,
        o.created_at,
        ROW_NUMBER() OVER (
            PARTITION BY o.order_id
            ORDER BY o.created_at DESC
        ) AS row_num
    FROM raw_orders AS o
    LEFT JOIN raw_customers AS c
        ON o.customer_id = c.customer_id
    WHERE o.status != 'cancelled'
      AND o.amount > 0
)
SELECT
    order_id,
    customer_id,
    region,
    amount,
    status,
    created_at
FROM ranked_orders
WHERE row_num = 1;
"""

CLEAN_PIPELINE_JOBS = {
    "jobs": [
        {
            "job_name": "build_stg_orders_cleaned",
            "schedule": "0 * * * *",
            "sql_model": "sql_models/01_stg_orders_cleaned.sql",
            "upstream_tables": ["raw_orders", "raw_customers"],
            "downstream_tables": ["stg_orders_cleaned"],
            "last_run_status": "success",
            "last_run_duration_seconds": 42,
            "last_run_at": "2024-01-30T06:00:12Z",
        },
        {
            "job_name": "build_fct_daily_revenue",
            "schedule": "15 * * * *",
            "sql_model": "sql_models/02_fct_daily_revenue.sql",
            "upstream_tables": ["stg_orders_cleaned"],
            "downstream_tables": ["fct_daily_revenue"],
            "last_run_status": "success",
            "last_run_duration_seconds": 18,
            "last_run_at": "2024-01-30T06:15:07Z",
        },
        {
            "job_name": "build_fct_daily_revenue_rolling_avg",
            "schedule": "30 * * * *",
            "sql_model": "sql_models/03_fct_daily_revenue_rolling_avg.sql",
            "upstream_tables": ["fct_daily_revenue"],
            "downstream_tables": ["mart_revenue_trends (view, not materialized)"],
            "last_run_status": "success",
            "last_run_duration_seconds": 9,
            "last_run_at": "2024-01-30T06:30:44Z",
        },
    ]
}


def restore_clean_files() -> None:
    STG_ORDERS_CLEANED_SQL_PATH.write_text(
        CLEAN_STG_ORDERS_CLEANED_SQL, encoding="utf-8"
    )
    PIPELINE_JOBS_PATH.write_text(
        json.dumps(CLEAN_PIPELINE_JOBS, indent=2) + "\n", encoding="utf-8"
    )


def reset_to_clean_baseline() -> dict[str, int]:
    from app.sandbox_data.seed import seed  # local import: avoids import cycles

    restore_clean_files()
    return seed()


if __name__ == "__main__":
    counts = reset_to_clean_baseline()
    print("Reset to clean baseline. Row counts:")
    for table, count in counts.items():
        print(f"  {table}: {count}")
