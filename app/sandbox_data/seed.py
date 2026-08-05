"""Seed script for the sandbox "fake company" data warehouse.

Generates ~200 realistic fake order rows (across 2 regions and a 30-day
window) into raw_orders / raw_customers, then executes the SQL models in
sql_models/ to materialize stg_orders_cleaned and fct_daily_revenue,
mimicking a dbt-style run.

Usage:
    python -m app.sandbox_data.seed
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from sqlalchemy import text

from app.sandbox_data import pipeline
from app.sandbox_data.models import Base, RawCustomer, RawOrder, get_engine

RANDOM_SEED = 42
REGIONS = ["US", "EU"]
CUSTOMERS_PER_REGION = 20
NUM_UNIQUE_ORDERS = 194
NUM_DUPLICATE_EVENTS = 6  # simulate late-arriving duplicate raw events
ZERO_AMOUNT_PROBABILITY = 0.015  # a few bad rows a real pipeline must filter

ORDER_WINDOW_START = date(2024, 1, 1)
ORDER_WINDOW_DAYS = 30

STATUS_WEIGHTS = {
    "completed": 0.70,
    "pending": 0.13,
    "cancelled": 0.12,
    "refunded": 0.05,
}


def _weighted_status() -> str:
    return random.choices(
        list(STATUS_WEIGHTS.keys()), weights=list(STATUS_WEIGHTS.values())
    )[0]


def _random_datetime_in_window() -> datetime:
    day_offset = random.randint(0, ORDER_WINDOW_DAYS - 1)
    seconds_offset = random.randint(0, 24 * 60 * 60 - 1)
    window_start = datetime.combine(ORDER_WINDOW_START, datetime.min.time())
    return window_start + timedelta(days=day_offset, seconds=seconds_offset)


def build_customers() -> list[dict]:
    customers = []
    customer_id = 1
    for region in REGIONS:
        for _ in range(CUSTOMERS_PER_REGION):
            signup_date = ORDER_WINDOW_START - timedelta(days=random.randint(30, 730))
            customers.append(
                {
                    "customer_id": customer_id,
                    "region": region,
                    "signup_date": signup_date,
                }
            )
            customer_id += 1
    return customers


def build_orders(customers: list[dict]) -> list[dict]:
    """Builds NUM_UNIQUE_ORDERS orders plus a handful of duplicate raw
    events for realism, then shuffles ingestion order."""
    customer_ids = [c["customer_id"] for c in customers]
    orders: list[dict] = []

    for order_id in range(1001, 1001 + NUM_UNIQUE_ORDERS):
        amount = round(random.uniform(15.0, 450.0), 2)
        if random.random() < ZERO_AMOUNT_PROBABILITY:
            amount = 0.0  # simulated bad row, filtered out in staging
        orders.append(
            {
                "order_id": order_id,
                "customer_id": random.choice(customer_ids),
                "amount": amount,
                "status": _weighted_status(),
                "created_at": _random_datetime_in_window(),
            }
        )

    duplicate_targets = random.sample(orders, NUM_DUPLICATE_EVENTS)
    for original in duplicate_targets:
        orders.append(
            {
                "order_id": original["order_id"],
                "customer_id": original["customer_id"],
                "amount": original["amount"],
                "status": original["status"],
                "created_at": original["created_at"]
                + timedelta(minutes=random.randint(5, 240)),
            }
        )

    random.shuffle(orders)
    return orders


def seed() -> dict[str, int]:
    random.seed(RANDOM_SEED)
    engine = get_engine()

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    customers = build_customers()
    orders = build_orders(customers)

    with engine.begin() as connection:
        connection.execute(RawCustomer.__table__.insert(), customers)
        connection.execute(RawOrder.__table__.insert(), orders)

        pipeline.rebuild_stg_orders_cleaned(connection)
        pipeline.rebuild_fct_daily_revenue(connection)

        # Executed (not materialized) purely to confirm the downstream
        # mart model reads cleanly against real data.
        rolling_avg_rows = pipeline.run_sql_model(
            connection, "03_fct_daily_revenue_rolling_avg.sql"
        )

    with engine.connect() as connection:
        counts = {
            "raw_customers": connection.execute(
                text("SELECT COUNT(*) FROM raw_customers")
            ).scalar(),
            "raw_orders": connection.execute(
                text("SELECT COUNT(*) FROM raw_orders")
            ).scalar(),
            "stg_orders_cleaned": connection.execute(
                text("SELECT COUNT(*) FROM stg_orders_cleaned")
            ).scalar(),
            "fct_daily_revenue": connection.execute(
                text("SELECT COUNT(*) FROM fct_daily_revenue")
            ).scalar(),
        }
    counts["fct_daily_revenue_rolling_avg (validated, not materialized)"] = len(
        rolling_avg_rows
    )
    return counts


if __name__ == "__main__":
    row_counts = seed()
    print("Seed complete. Row counts:")
    for table, count in row_counts.items():
        print(f"  {table}: {count}")
