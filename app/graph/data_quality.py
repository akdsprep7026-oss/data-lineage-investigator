"""Direct data-quality checks against the sandbox warehouse
(app/sandbox_data), used by data_quality_node.

Deliberately simple and deterministic -- no LLM involved, just row
counts, duplicate-id detection, duplicate-transaction detection, and
null counts run straight against whichever tables lineage_agent_node
flagged as relevant. This is meant to catch data-hygiene problems an
LLM reading SQL text alone wouldn't necessarily surface, complementing
sql_analysis_node's review of the transformation logic itself.
"""

from __future__ import annotations

from typing import Optional, TypedDict

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.sandbox_data.models import get_engine

# Candidate id-like columns to check for duplicates, in priority order.
# Not every table has one of these (e.g. fct_daily_revenue doesn't), in
# which case the duplicate-id check is simply skipped for that table.
ID_COLUMN_CANDIDATES = ("order_id", "customer_id", "id")

# Candidate event-timestamp columns, used to bucket rows by day for the
# duplicate-transaction check below. Kept in sync with
# app/graph/validation.py's TIMESTAMP_COLUMN_CANDIDATES, since incident
# 3 renames raw_orders.created_at and both modules need to keep working
# against whichever name is actually live.
TIMESTAMP_COLUMN_CANDIDATES = ("created_at", "order_created_at")

# Columns that, together with an id column and a timestamp column,
# plausibly identify "the same real-world transaction" even when it's
# been given a new id -- e.g. the same customer paying the same amount
# on the same day. Any one of these missing just means the duplicate-
# transaction check is skipped for that table; the plain duplicate-id
# check above still runs regardless.
TRANSACTION_GROUPING_COLUMNS = ("customer_id", "amount")


class TableQualityReport(TypedDict):
    table: str
    row_count: int
    duplicate_id_column: Optional[str]
    duplicate_id_count: int
    # Number of (customer_id, amount, day) groups that share more than
    # one distinct id -- i.e. the same transaction re-emitted under a
    # *different* id, which duplicate_id_count above cannot catch since
    # it only counts a repeated id as a duplicate. None when the table
    # lacks a usable id/timestamp/grouping column combination.
    duplicate_transaction_groups: Optional[int]
    null_counts: dict[str, int]


def _pick_id_column(columns: list[str]) -> Optional[str]:
    for candidate in ID_COLUMN_CANDIDATES:
        if candidate in columns:
            return candidate
    return None


def _pick_timestamp_column(columns: list[str]) -> Optional[str]:
    for candidate in TIMESTAMP_COLUMN_CANDIDATES:
        if candidate in columns:
            return candidate
    return None


def run_basic_checks(table_name: str, engine: Optional[Engine] = None) -> TableQualityReport:
    """Runs row-count, duplicate-id, duplicate-transaction, and null-
    count checks against one table in the sandbox warehouse.

    Raises ValueError if `table_name` isn't an actual table in the
    sandbox warehouse (e.g. a mart/view name that's never materialized)
    -- callers should treat that as "nothing to check" and move on.
    """
    engine = engine or get_engine()
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        raise ValueError(f"{table_name!r} is not a table in the sandbox warehouse")
    columns = [col["name"] for col in inspector.get_columns(table_name)]

    with engine.connect() as connection:
        row_count = connection.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()

        id_column = _pick_id_column(columns)
        duplicate_id_count = 0
        if id_column:
            duplicate_id_count = connection.execute(
                text(
                    f"SELECT COUNT(*) FROM ("
                    f"SELECT {id_column} FROM {table_name} "
                    f"GROUP BY {id_column} HAVING COUNT(*) > 1"
                    f")"
                )
            ).scalar()

        duplicate_transaction_groups: Optional[int] = None
        timestamp_column = _pick_timestamp_column(columns)
        has_grouping_columns = all(col in columns for col in TRANSACTION_GROUPING_COLUMNS)
        if id_column and timestamp_column and has_grouping_columns:
            grouping_columns = ", ".join(TRANSACTION_GROUPING_COLUMNS)
            duplicate_transaction_groups = connection.execute(
                text(
                    f"SELECT COUNT(*) FROM ("
                    f"SELECT {grouping_columns}, DATE({timestamp_column}) AS day "
                    f"FROM {table_name} "
                    f"GROUP BY {grouping_columns}, DATE({timestamp_column}) "
                    f"HAVING COUNT(DISTINCT {id_column}) > 1"
                    f")"
                )
            ).scalar()

        null_counts = {
            column: connection.execute(
                text(f"SELECT COUNT(*) FROM {table_name} WHERE {column} IS NULL")
            ).scalar()
            for column in columns
        }
        null_counts = {column: count for column, count in null_counts.items() if count}

    return TableQualityReport(
        table=table_name,
        row_count=row_count,
        duplicate_id_column=id_column,
        duplicate_id_count=duplicate_id_count,
        duplicate_transaction_groups=duplicate_transaction_groups,
        null_counts=null_counts,
    )
