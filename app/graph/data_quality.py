"""Direct data-quality checks against the sandbox warehouse
(app/sandbox_data), used by data_quality_node.

Deliberately simple and deterministic -- no LLM involved, just row
counts, duplicate-id detection, and null counts run straight against
whichever tables lineage_agent_node flagged as relevant. This is meant
to catch data-hygiene problems an LLM reading SQL text alone wouldn't
necessarily surface, complementing sql_analysis_node's review of the
transformation logic itself.
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


class TableQualityReport(TypedDict):
    table: str
    row_count: int
    duplicate_id_column: Optional[str]
    duplicate_id_count: int
    null_counts: dict[str, int]


def _pick_id_column(columns: list[str]) -> Optional[str]:
    for candidate in ID_COLUMN_CANDIDATES:
        if candidate in columns:
            return candidate
    return None


def run_basic_checks(table_name: str, engine: Optional[Engine] = None) -> TableQualityReport:
    """Runs row-count, duplicate-id, and null-count checks against one
    table in the sandbox warehouse.

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
        null_counts=null_counts,
    )
