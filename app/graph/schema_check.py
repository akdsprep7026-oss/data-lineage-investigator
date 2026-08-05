"""Static schema-vs-SQL check used by schema_agent_node.

Parses which `<table>.<column>` references a SQL model's FROM/JOIN
aliases resolve to, and compares that against the *live* schema of
each referenced table in the sandbox warehouse. Any column the SQL
references that the table doesn't actually have right now is a
mismatch -- exactly the failure mode a breaking upstream rename (e.g.
incident 3's raw_orders.created_at -> order_created_at) introduces.

This is deliberately static and column-name-only, which makes it a
different kind of check than the other two schema-adjacent ones already
in the codebase:

  - sql_analysis_node (app/graph/sql_review.py) has an LLM read the SQL
    for *logic* bugs (bad joins, missing filters); it isn't looking at
    the live database at all.
  - validation_node's _check_schema_change (app/graph/validation.py)
    re-*executes* the model against the live schema, which is a more
    direct confirmation but only runs once a hypothesis already exists
    to check.

This module fills the gap between them: a cheap, no-LLM, no-execution
check that can run as part of ordinary evidence gathering and flag a
column mismatch before any hypothesis has been proposed.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.sandbox_data.models import get_engine

_SQL_COMMENT_RE = re.compile(r"--.*")
_FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_ALIAS_COLUMN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")

# Not real aliases -- a "FROM/JOIN <table> <token>" match where <token>
# is actually the start of the next clause (this shouldn't come up in
# practice given the sandbox's SQL style, but excluding SQL keywords
# keeps the alias map from mis-resolving one as a table's own alias).
_RESERVED_TOKENS = {"where", "on", "as", "left", "inner", "right", "full", "join", "group", "order"}


class ColumnMismatch(NamedTuple):
    table: str
    column: str
    referenced_as: str  # the "alias.column" text as it appears in the SQL


def _strip_comments(sql_text: str) -> str:
    return _SQL_COMMENT_RE.sub("", sql_text)


def _alias_table_map(sql_text: str) -> dict[str, str]:
    """Maps each alias used in a FROM/JOIN clause to the table it
    refers to (e.g. {"o": "raw_orders", "c": "raw_customers"}), plus
    each such table to itself so an unaliased `table.column` reference
    resolves too."""
    mapping: dict[str, str] = {}
    for table, alias in _FROM_JOIN_RE.findall(sql_text):
        mapping[table] = table
        if alias.lower() not in _RESERVED_TOKENS:
            mapping[alias] = table
    return mapping


def find_schema_mismatches(
    sql_text: str, engine: Optional[Engine] = None
) -> list[ColumnMismatch]:
    """Checks every `<alias>.<column>` reference in `sql_text` against
    the live schema of the table that alias resolves to.

    Returns one ColumnMismatch per referenced column that doesn't exist
    on that table right now. A table the SQL references that doesn't
    exist in the warehouse at all is skipped here -- that is a
    "table_missing" condition data_quality_node's checks already
    surface -- so this stays focused on the specific failure mode of a
    column being renamed or dropped out from under an otherwise-intact
    table.
    """
    engine = engine or get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    text = _strip_comments(sql_text)
    alias_to_table = _alias_table_map(text)

    columns_by_table: dict[str, set[str]] = {}
    mismatches: list[ColumnMismatch] = []
    seen: set[tuple[str, str]] = set()

    for alias, column in _ALIAS_COLUMN_RE.findall(text):
        table = alias_to_table.get(alias)
        if table is None or table not in existing_tables:
            continue
        if table not in columns_by_table:
            columns_by_table[table] = {col["name"] for col in inspector.get_columns(table)}
        if column in columns_by_table[table]:
            continue
        key = (table, column)
        if key in seen:
            continue
        seen.add(key)
        mismatches.append(
            ColumnMismatch(table=table, column=column, referenced_as=f"{alias}.{column}")
        )

    return mismatches
