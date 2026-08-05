"""Incident 1: join bug (revenue undercounting).

Simulates a customer-dimension sync lag: a handful of brand-new
customers place orders before their customer_id has propagated into
raw_customers. sql_models/01_stg_orders_cleaned.sql is regressed from a
LEFT JOIN (which keeps such orders, falling back to region='UNKNOWN')
to an INNER JOIN, which silently drops them -- undercounting revenue
for that day.

Apply: python -m app.sandbox_data.incidents.incident_01_join_bug
   or: python -m app.sandbox_data.incidents.manage apply 1
Reset: python -m app.sandbox_data.incidents.manage reset
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from app.sandbox_data import pipeline
from app.sandbox_data.incidents import common
from app.sandbox_data.models import RawOrder, get_engine

INCIDENT_DAY = "2024-01-20"
ORPHAN_CUSTOMER_IDS = [9001, 9002, 9003, 9004, 9005]

BUGGY_STG_ORDERS_CLEANED_SQL = """-- stg_orders_cleaned.sql  [INCIDENT 1: join bug]
--
-- BUG: this INNER JOIN silently drops any order whose customer_id has not
-- yet landed in raw_customers (e.g. a brand-new signup), instead of
-- keeping the order via a LEFT JOIN with a fallback region. This
-- undercounts revenue for any day with new-customer orders.

WITH ranked_orders AS (
    SELECT
        o.order_id,
        o.customer_id,
        c.region,
        o.amount,
        o.status,
        o.created_at,
        ROW_NUMBER() OVER (
            PARTITION BY o.order_id
            ORDER BY o.created_at DESC
        ) AS row_num
    FROM raw_orders AS o
    INNER JOIN raw_customers AS c
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


def apply() -> None:
    common.reset_to_clean_baseline()
    engine = get_engine()

    orphan_orders = [
        {
            "order_id": 80000 + i,
            "customer_id": customer_id,
            "amount": round(120.0 + 15 * i, 2),
            "status": "completed",
            "created_at": datetime.fromisoformat(
                f"{INCIDENT_DAY}T{9 + i:02d}:00:00"
            ),
        }
        for i, customer_id in enumerate(ORPHAN_CUSTOMER_IDS)
    ]
    injected_total = sum(o["amount"] for o in orphan_orders)

    with engine.begin() as connection:
        connection.execute(RawOrder.__table__.insert(), orphan_orders)

    common.STG_ORDERS_CLEANED_SQL_PATH.write_text(
        BUGGY_STG_ORDERS_CLEANED_SQL, encoding="utf-8"
    )

    with engine.begin() as connection:
        pipeline.rebuild_stg_orders_cleaned(connection)
        pipeline.rebuild_fct_daily_revenue(connection)

        raw_total = connection.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM raw_orders "
                "WHERE DATE(created_at) = :d AND status = 'completed'"
            ),
            {"d": INCIDENT_DAY},
        ).scalar()
        fct_total = connection.execute(
            text(
                "SELECT COALESCE(SUM(total_revenue), 0) FROM fct_daily_revenue "
                "WHERE date = :d"
            ),
            {"d": INCIDENT_DAY},
        ).scalar()

    print("Incident 1 (join bug) applied.")
    print(
        f"  Injected {len(orphan_orders)} orphan orders on {INCIDENT_DAY} "
        f"(${injected_total:,.2f}) for customer_ids not present in raw_customers."
    )
    print("  sql_models/01_stg_orders_cleaned.sql regressed to an INNER JOIN.")
    print(f"  raw_orders completed total for {INCIDENT_DAY}: ${raw_total:,.2f}")
    print(f"  fct_daily_revenue total for {INCIDENT_DAY}:    ${fct_total:,.2f}  <- undercounted")


if __name__ == "__main__":
    apply()
