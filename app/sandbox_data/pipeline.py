"""Shared helpers to (re)materialize stg_orders_cleaned / fct_daily_revenue
by executing the .sql files in sql_models/ against whatever raw data
currently exists in the sandbox warehouse.

Used by both seed.py (initial load) and the incident scripts in
incidents/ (which mutate raw data and/or the SQL model files themselves,
then rebuild the affected downstream table(s) to make the bug's effect
show up in the data).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.sandbox_data.models import FctDailyRevenue, StgOrdersCleaned

SANDBOX_DIR = Path(__file__).resolve().parent
SQL_MODELS_DIR = SANDBOX_DIR / "sql_models"


def run_sql_model(connection: Connection, filename: str) -> list[dict]:
    """Reads a .sql model file from sql_models/ and executes it, returning
    rows as plain dicts."""
    sql_text = (SQL_MODELS_DIR / filename).read_text(encoding="utf-8")
    result = connection.execute(text(sql_text))
    return [dict(row._mapping) for row in result]


def parse_column(rows: list[dict], column: str, parser) -> None:
    """Raw text() queries bypass SQLAlchemy's type decoders, so SQLite
    hands back plain strings for DATE/DATETIME columns. Parse them back
    into Python date/datetime objects before re-inserting via the ORM
    models, which expect native types."""
    for row in rows:
        if isinstance(row.get(column), str):
            row[column] = parser(row[column])


def rebuild_stg_orders_cleaned(connection: Connection) -> int:
    """Re-runs 01_stg_orders_cleaned.sql and replaces the contents of
    stg_orders_cleaned. Raises if the model SQL fails (e.g. an upstream
    schema change broke it) -- callers that want to simulate a failed
    job should catch the exception themselves."""
    connection.execute(StgOrdersCleaned.__table__.delete())
    rows = run_sql_model(connection, "01_stg_orders_cleaned.sql")
    parse_column(rows, "created_at", datetime.fromisoformat)
    if rows:
        connection.execute(StgOrdersCleaned.__table__.insert(), rows)
    return len(rows)


def rebuild_fct_daily_revenue(connection: Connection) -> int:
    """Re-runs 02_fct_daily_revenue.sql and replaces the contents of
    fct_daily_revenue."""
    connection.execute(FctDailyRevenue.__table__.delete())
    rows = run_sql_model(connection, "02_fct_daily_revenue.sql")
    parse_column(rows, "date", date.fromisoformat)
    if rows:
        connection.execute(FctDailyRevenue.__table__.insert(), rows)
    return len(rows)


def rebuild_all(connection: Connection) -> dict[str, int]:
    stg_count = rebuild_stg_orders_cleaned(connection)
    fct_count = rebuild_fct_daily_revenue(connection)
    return {"stg_orders_cleaned": stg_count, "fct_daily_revenue": fct_count}
