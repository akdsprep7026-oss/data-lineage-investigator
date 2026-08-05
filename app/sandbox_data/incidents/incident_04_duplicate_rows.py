"""Incident 4: duplicate-row bug (revenue inflation).

Simulates an upstream checkout-retry bug: ~15 completed orders from
2024-01-15 get re-emitted into raw_orders with brand-new order_id
values for what is really the same underlying transaction.
sql_models/01_stg_orders_cleaned.sql only de-duplicates on order_id
(see incident-free ROW_NUMBER() PARTITION BY order_id logic), so these
new rows look like distinct legitimate orders and are not filtered
out, inflating that day's revenue in fct_daily_revenue.

Apply: python -m app.sandbox_data.incidents.incident_04_duplicate_rows
   or: python -m app.sandbox_data.incidents.manage apply 4
Reset: python -m app.sandbox_data.incidents.manage reset
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from app.sandbox_data import pipeline
from app.sandbox_data.incidents import common
from app.sandbox_data.models import RawOrder, get_engine

INCIDENT_DAY = "2024-01-15"
DUPLICATE_ORDER_ID_START = 90001
NUM_DUPLICATES = 15


def apply() -> None:
    common.reset_to_clean_baseline()
    engine = get_engine()

    with engine.begin() as connection:
        before_total = connection.execute(
            text(
                "SELECT COALESCE(SUM(total_revenue), 0) FROM fct_daily_revenue "
                "WHERE date = :d"
            ),
            {"d": INCIDENT_DAY},
        ).scalar()

        source_rows = connection.execute(
            text(
                "SELECT customer_id, amount, status, created_at FROM raw_orders "
                "WHERE DATE(created_at) = :d AND status = 'completed' "
                "LIMIT :n"
            ),
            {"d": INCIDENT_DAY, "n": NUM_DUPLICATES},
        ).mappings().all()

        duplicate_rows = [
            {
                "order_id": DUPLICATE_ORDER_ID_START + i,
                "customer_id": row["customer_id"],
                "amount": row["amount"],
                "status": row["status"],
                "created_at": datetime.fromisoformat(row["created_at"]),
            }
            for i, row in enumerate(source_rows)
        ]
        connection.execute(RawOrder.__table__.insert(), duplicate_rows)

        pipeline.rebuild_stg_orders_cleaned(connection)
        pipeline.rebuild_fct_daily_revenue(connection)

        after_total = connection.execute(
            text(
                "SELECT COALESCE(SUM(total_revenue), 0) FROM fct_daily_revenue "
                "WHERE date = :d"
            ),
            {"d": INCIDENT_DAY},
        ).scalar()

    injected_total = sum(r["amount"] for r in duplicate_rows)

    print("Incident 4 (duplicate-row bug) applied.")
    print(
        f"  Re-inserted {len(duplicate_rows)} orders from {INCIDENT_DAY} under new "
        f"order_ids {DUPLICATE_ORDER_ID_START}-"
        f"{DUPLICATE_ORDER_ID_START + len(duplicate_rows) - 1} "
        f"(phantom revenue: ${injected_total:,.2f})."
    )
    print(f"  fct_daily_revenue total for {INCIDENT_DAY}: ${before_total:,.2f} -> ${after_total:,.2f}")


if __name__ == "__main__":
    apply()
