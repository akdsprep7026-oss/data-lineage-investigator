"""Direct data-quality checks against the sandbox warehouse
(app/sandbox_data), used by data_quality_node.

Deliberately simple and deterministic -- no LLM involved, just row
counts, duplicate-id detection, duplicate-transaction detection, and
null counts run straight against whichever tables lineage_agent_node
flagged as relevant. This is meant to catch data-hygiene problems an
LLM reading SQL text alone wouldn't necessarily surface, complementing
sql_analysis_node's review of the transformation logic itself.

As of Step 8 the warehouse is not touched directly here. Every read goes
through the MCP server in app/mcp_servers/postgres_server.py, using the
three tools it publishes: get_schema for the column list,
check_row_count for the row count, and query_table for the rows the
duplicate and null checks are computed from. The checks themselves are
unchanged; only how this module gets at the data has.

That split -- fetch over MCP, aggregate here -- is deliberate. The MCP
tools stay a small, general, read-only window onto the warehouse
(exactly the three the Step 8 spec calls for) that any MCP client can
use, rather than growing a bespoke `count_the_duplicates_for_me` tool
that only makes sense to this one caller. It costs three tool calls per
table instead of a handful of aggregate queries, which for a warehouse
of a few hundred rows is free.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any, Optional, TypedDict

from app.mcp_servers.client import POSTGRES_SERVER, call_tool

logger = logging.getLogger(__name__)

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


def _day(timestamp: Any) -> str:
    """The calendar day a timestamp falls on.

    Equivalent to SQLite's DATE() for the values this warehouse stores:
    every supported timestamp format ('2024-01-15 13:45:22.000000' as
    SQLite hands it back, or an ISO 'T'-separated string) begins with
    YYYY-MM-DD.
    """
    return str(timestamp)[:10]


def _count_repeated_ids(rows: list[dict[str, Any]], id_column: str) -> int:
    """How many distinct ids appear on more than one row."""
    occurrences = Counter(row[id_column] for row in rows)
    return sum(1 for count in occurrences.values() if count > 1)


def _count_duplicate_transaction_groups(
    rows: list[dict[str, Any]], id_column: str, timestamp_column: str
) -> int:
    """How many customer/amount/day groups carry more than one id --
    i.e. the same transaction recorded under multiple ids."""
    ids_by_group: defaultdict[tuple, set] = defaultdict(set)
    for row in rows:
        group = (
            *(row[column] for column in TRANSACTION_GROUPING_COLUMNS),
            _day(row[timestamp_column]),
        )
        ids_by_group[group].add(row[id_column])
    return sum(1 for ids in ids_by_group.values() if len(ids) > 1)


def run_basic_checks(table_name: str) -> TableQualityReport:
    """Runs row-count, duplicate-id, duplicate-transaction, and null-
    count checks against one table in the sandbox warehouse, reading it
    through the warehouse MCP server.

    Raises ValueError if `table_name` isn't an actual table in the
    sandbox warehouse (e.g. a mart/view name that's never materialized)
    -- callers should treat that as "nothing to check" and move on.
    """
    schema = call_tool(POSTGRES_SERVER, "get_schema", {"table_name": table_name})
    if not schema["exists"]:
        raise ValueError(f"{table_name!r} is not a table in the sandbox warehouse")
    columns = [column["name"] for column in schema["columns"]]

    count = call_tool(POSTGRES_SERVER, "check_row_count", {"table_name": table_name})
    row_count = count["row_count"]

    table = call_tool(POSTGRES_SERVER, "query_table", {"table_name": table_name})
    rows = table["rows"]
    if table["truncated"]:
        # Can't happen for the sandbox (its largest table is ~215 rows
        # against a 5000-row ceiling), but the counts below would
        # silently under-report if it ever did, so say so loudly.
        logger.warning(
            "%s returned a truncated row set (%d of %d rows); duplicate and "
            "null counts below are computed from the truncated set.",
            table_name,
            len(rows),
            row_count,
        )

    id_column = _pick_id_column(columns)
    duplicate_id_count = _count_repeated_ids(rows, id_column) if id_column else 0

    duplicate_transaction_groups: Optional[int] = None
    timestamp_column = _pick_timestamp_column(columns)
    has_grouping_columns = all(col in columns for col in TRANSACTION_GROUPING_COLUMNS)
    if id_column and timestamp_column and has_grouping_columns:
        duplicate_transaction_groups = _count_duplicate_transaction_groups(
            rows, id_column, timestamp_column
        )

    null_counts = {
        column: sum(1 for row in rows if row[column] is None) for column in columns
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
