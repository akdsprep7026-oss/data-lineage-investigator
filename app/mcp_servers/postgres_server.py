"""MCP server exposing read-only access to the sandbox data warehouse.

Run it as a standalone MCP server over stdio:

    python -m app.mcp_servers.postgres_server

Three tools are exposed (see each tool's docstring for its schema --
MCPServer publishes the docstring as the tool's `description` and derives
the input schema from the type hints, so those docstrings are literally
what an MCP client is shown):

    get_schema(table_name)            columns of one table
    check_row_count(table_name)       COUNT(*) of one table
    query_table(table_name, filters)  rows, optionally equality-filtered

Every tool is read-only: only SELECT statements are issued, table and
column names are validated against the live schema before being
interpolated, and filter *values* are always bound parameters, never
formatted into the SQL string. An agent connected to this server can
therefore look at the warehouse it's investigating but cannot alter it.

A note on the name: the module is called `postgres_server` for the role
it plays -- the warehouse-database MCP server -- while the sandbox
warehouse itself has been a local SQLite file since Step 1 (see
app/sandbox_data/models.py), so that's what it connects to. Swapping in
a real Postgres warehouse means changing the engine that
`_warehouse_engine()` returns and nothing else; the tool schemas below
are dialect-independent. The application's *own* Postgres database (the
`investigations` table, see app/db/) is a separate concern and is
deliberately not exposed here: it's the agents' bookkeeping, not
evidence about the incident.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.sandbox_data.models import get_engine

SERVER_NAME = "sandbox-warehouse"

# query_table's ceiling on rows returned in one call, so a tool call can
# never try to serialize an unbounded table into a single MCP response.
# The sandbox's largest table is ~215 rows, so this is never reached in
# practice; the response carries a `truncated` flag for the case where
# it is.
MAX_ROWS_RETURNED = 5000

server = MCPServer(
    SERVER_NAME,
    instructions=(
        "Read-only access to the sandbox data warehouse under "
        "investigation. Use get_schema to see a table's columns, "
        "check_row_count for its size, and query_table to read its rows "
        "(optionally filtered on exact column values)."
    ),
)

_ENGINE: Optional[Engine] = None


def _warehouse_engine() -> Engine:
    """The sandbox warehouse engine, created once per server process.

    Caching it is safe even though the warehouse changes underneath us
    (the incident scripts in app/sandbox_data/incidents/ drop, rebuild
    and ALTER tables in a *different* process while this server is
    running): every tool below opens a fresh connection and a fresh
    Inspector, so each call starts a new read transaction and reflects
    the schema anew rather than reusing a cached snapshot.
    """
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = get_engine()
    return _ENGINE


def _jsonable(value: Any) -> Any:
    """Coerces a driver value into something JSON-serializable.

    A no-op for the sandbox today -- SQLite hands back plain strings for
    its DateTime/Date columns -- but it keeps the tool contract honest
    if the engine is ever pointed at a backend with richer types.
    """
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


def _table_columns(table_name: str) -> Optional[list[dict[str, Any]]]:
    """The columns of `table_name`, or None if there's no such table."""
    inspector = inspect(_warehouse_engine())
    if table_name not in set(inspector.get_table_names()):
        return None
    return [
        {
            "name": column["name"],
            "type": str(column["type"]),
            "nullable": bool(column["nullable"]),
            "primary_key": bool(column.get("primary_key")),
        }
        for column in inspector.get_columns(table_name)
    ]


@server.tool()
def get_schema(table_name: str) -> dict[str, Any]:
    """Describe the live schema of one table in the sandbox warehouse.

    MCP tool schema
    ---------------
    Input:
      table_name (string, required) -- name of the table to describe.

    Output (JSON object):
      table   (string)  the table that was asked about.
      exists  (boolean) false if no such table exists right now; the
                        caller should treat that as "nothing to inspect"
                        rather than as an error, since an incident can
                        legitimately remove a table.
      columns (array)   one object per column, in the table's own column
                        order: {name, type, nullable, primary_key}.
                        Empty when exists is false.

    Reads the schema fresh on every call, so a rename or drop applied
    after this server started (e.g. incident 3 renaming
    raw_orders.created_at) is reflected immediately.
    """
    columns = _table_columns(table_name)
    return {
        "table": table_name,
        "exists": columns is not None,
        "columns": columns or [],
    }


@server.tool()
def check_row_count(table_name: str) -> dict[str, Any]:
    """Count the rows currently in one table of the sandbox warehouse.

    MCP tool schema
    ---------------
    Input:
      table_name (string, required) -- name of the table to count.

    Output (JSON object):
      table     (string)  the table that was asked about.
      exists    (boolean) false if no such table exists right now.
      row_count (integer) COUNT(*) for the table, or 0 when exists is
                          false. Check `exists` before reading this: an
                          absent table and an empty one are different
                          findings.
    """
    if _table_columns(table_name) is None:
        return {"table": table_name, "exists": False, "row_count": 0}

    engine = _warehouse_engine()
    with engine.connect() as connection:
        row_count = connection.execute(
            text(f"SELECT COUNT(*) FROM {table_name}")
        ).scalar()
    return {"table": table_name, "exists": True, "row_count": int(row_count or 0)}


@server.tool()
def query_table(
    table_name: str, filters: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Read rows from one table of the sandbox warehouse.

    MCP tool schema
    ---------------
    Input:
      table_name (string, required) -- name of the table to read.
      filters    (object, optional) -- column name -> exact value to
                 match. Values are compared with `=`, except null, which
                 is compared with `IS NULL`. Multiple entries are ANDed.
                 Omit or pass {} to read the whole table. Every key must
                 be a real column of the table; an unknown column is an
                 error rather than a silently ignored filter, so a
                 caller can't mistake "filter didn't apply" for "no rows
                 matched".

    Output (JSON object):
      table          (string)  the table that was read.
      exists         (boolean) false if no such table exists right now.
      columns        (array)   the column names, in table order.
      rows           (array)   one object per row, keyed by column name.
      returned_rows  (integer) len(rows).
      truncated      (boolean) true if the table had more matching rows
                               than MAX_ROWS_RETURNED and the result was
                               cut short. Never true for the sandbox,
                               whose largest table is ~215 rows.

    Read-only, and injection-safe: `table_name` and every filter key are
    validated against the live schema before being interpolated into the
    SQL, and filter values are passed as bound parameters.
    """
    columns = _table_columns(table_name)
    if columns is None:
        return {
            "table": table_name,
            "exists": False,
            "columns": [],
            "rows": [],
            "returned_rows": 0,
            "truncated": False,
        }

    column_names = [column["name"] for column in columns]
    filters = filters or {}
    unknown = [name for name in filters if name not in column_names]
    if unknown:
        raise ValueError(
            f"{table_name} has no column(s) {', '.join(sorted(unknown))}. "
            f"Available columns: {', '.join(column_names)}."
        )

    conditions: list[str] = []
    parameters: dict[str, Any] = {}
    for index, (column, value) in enumerate(filters.items()):
        if value is None:
            conditions.append(f"{column} IS NULL")
            continue
        placeholder = f"filter_{index}"
        conditions.append(f"{column} = :{placeholder}")
        parameters[placeholder] = value

    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    # One row over the limit, so `truncated` can distinguish "exactly at
    # the limit" from "there was more".
    statement = (
        f"SELECT {', '.join(column_names)} FROM {table_name}{where_clause} "
        f"LIMIT {MAX_ROWS_RETURNED + 1}"
    )

    engine = _warehouse_engine()
    with engine.connect() as connection:
        result = connection.execute(text(statement), parameters).mappings().all()

    truncated = len(result) > MAX_ROWS_RETURNED
    rows = [
        {column: _jsonable(row[column]) for column in column_names}
        for row in result[:MAX_ROWS_RETURNED]
    ]
    return {
        "table": table_name,
        "exists": True,
        "columns": column_names,
        "rows": rows,
        "returned_rows": len(rows),
        "truncated": truncated,
    }


if __name__ == "__main__":
    server.run("stdio")
