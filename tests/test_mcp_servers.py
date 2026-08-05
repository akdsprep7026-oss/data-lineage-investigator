"""Tests for the Step 8 MCP layer (app/mcp_servers/).

Two things are worth testing here, and they're different in kind.

The first is the published contract: each server really does advertise
the tools it's supposed to, with the documented parameters, over a real
MCP session. These tests go through app/mcp_servers/client.py, which
spawns the servers as subprocesses and speaks the protocol to them, so
they exercise the transport rather than just calling the tool functions
in-process.

The second, and the reason this file exists at all, is that the refactor
didn't change any answers. `run_basic_checks` used to issue aggregate
SQL against the warehouse directly; it now fetches rows through MCP
tools and aggregates them in Python. Those two routes have to agree
exactly, including in the awkward cases (a table with no id column, a
table whose timestamp column has been renamed underneath it, an incident
that plants duplicate transactions under fresh ids). So the tests below
recompute each report with the original SQL and compare.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from app.graph.data_quality import (
    TRANSACTION_GROUPING_COLUMNS,
    TableQualityReport,
    _pick_id_column,
    _pick_timestamp_column,
    run_basic_checks,
)
from app.mcp_servers.client import (
    POSTGRES_SERVER,
    RETRIEVAL_SERVER,
    MCPToolError,
    call_tool,
    list_tools,
)
from app.retrieval.ingest import ingest
from app.retrieval.retriever import retrieve
from app.sandbox_data.incidents import (
    common,
    incident_03_schema_change,
    incident_04_duplicate_rows,
)
from app.sandbox_data.models import get_engine

SANDBOX_TABLES = ["raw_customers", "raw_orders", "stg_orders_cleaned", "fct_daily_revenue"]


def _direct_sql_report(table_name: str) -> TableQualityReport:
    """The pre-Step-8 implementation of run_basic_checks, kept here as
    the reference to compare the MCP-backed one against: aggregate SQL
    issued straight at the warehouse, no MCP involved."""
    engine = get_engine()
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        raise ValueError(f"{table_name!r} is not a table in the sandbox warehouse")
    columns = [column["name"] for column in inspector.get_columns(table_name)]

    with engine.connect() as connection:
        row_count = connection.execute(
            text(f"SELECT COUNT(*) FROM {table_name}")
        ).scalar()

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

        duplicate_transaction_groups = None
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


def test_warehouse_server_publishes_its_three_tools():
    published = {tool["name"]: tool for tool in list_tools(POSTGRES_SERVER)}

    assert set(published) == {"get_schema", "check_row_count", "query_table"}
    # The docstrings are what an MCP client is shown as the tool
    # description, so an undocumented tool is a broken tool.
    for tool in published.values():
        assert tool["description"], f"{tool['name']} has no description"
        assert "MCP tool schema" in tool["description"]

    assert published["get_schema"]["input_schema"]["required"] == ["table_name"]
    assert published["check_row_count"]["input_schema"]["required"] == ["table_name"]
    query_schema = published["query_table"]["input_schema"]
    assert set(query_schema["properties"]) == {"table_name", "filters"}
    # filters is optional; table_name isn't.
    assert query_schema["required"] == ["table_name"]


def test_retrieval_server_publishes_the_retrieve_tool():
    published = {tool["name"]: tool for tool in list_tools(RETRIEVAL_SERVER)}

    assert set(published) == {"retrieve"}
    schema = published["retrieve"]["input_schema"]
    assert set(schema["properties"]) == {"query", "filter_type", "n_results"}
    assert schema["required"] == ["query"]
    assert "MCP tool schema" in published["retrieve"]["description"]


@pytest.mark.parametrize("table_name", SANDBOX_TABLES)
def test_get_schema_matches_sqlalchemy_reflection(table_name):
    result = call_tool(POSTGRES_SERVER, "get_schema", {"table_name": table_name})

    assert result["exists"] is True
    reflected = [
        column["name"] for column in inspect(get_engine()).get_columns(table_name)
    ]
    # Order matters as well as membership: run_basic_checks reports null
    # counts in column order, and that string ends up in the evidence.
    assert [column["name"] for column in result["columns"]] == reflected


def test_get_schema_reports_a_table_that_does_not_exist():
    result = call_tool(POSTGRES_SERVER, "get_schema", {"table_name": "mart_revenue_trends"})

    assert result == {"table": "mart_revenue_trends", "exists": False, "columns": []}


@pytest.mark.parametrize("table_name", SANDBOX_TABLES)
def test_check_row_count_matches_direct_sql(table_name):
    result = call_tool(POSTGRES_SERVER, "check_row_count", {"table_name": table_name})

    with get_engine().connect() as connection:
        expected = connection.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
    assert result == {"table": table_name, "exists": True, "row_count": expected}


def test_query_table_returns_every_row_when_unfiltered():
    result = call_tool(POSTGRES_SERVER, "query_table", {"table_name": "raw_orders"})

    with get_engine().connect() as connection:
        expected = connection.execute(text("SELECT COUNT(*) FROM raw_orders")).scalar()
    assert result["returned_rows"] == expected
    assert result["truncated"] is False
    assert set(result["rows"][0]) == set(result["columns"])


def test_query_table_filters_on_exact_column_values():
    result = call_tool(
        POSTGRES_SERVER,
        "query_table",
        {"table_name": "raw_orders", "filters": {"status": "cancelled"}},
    )

    with get_engine().connect() as connection:
        expected = connection.execute(
            text("SELECT COUNT(*) FROM raw_orders WHERE status = 'cancelled'")
        ).scalar()
    assert result["returned_rows"] == expected
    assert expected > 0, "the seeded warehouse should contain cancelled orders"
    assert all(row["status"] == "cancelled" for row in result["rows"])


def test_query_table_rejects_a_filter_on_an_unknown_column():
    """A silently-ignored filter would be worse than an error: the caller
    would read "no rows matched" out of a query that never filtered."""
    with pytest.raises(MCPToolError) as excinfo:
        call_tool(
            POSTGRES_SERVER,
            "query_table",
            {"table_name": "raw_orders", "filters": {"nonexistent_column": 1}},
        )

    assert "nonexistent_column" in str(excinfo.value)


def test_query_table_reports_a_table_that_does_not_exist():
    result = call_tool(POSTGRES_SERVER, "query_table", {"table_name": "not_a_table"})

    assert result["exists"] is False
    assert result["rows"] == []


@pytest.mark.parametrize("table_name", SANDBOX_TABLES)
def test_run_basic_checks_over_mcp_matches_direct_sql(table_name):
    """The Step 8 claim, table by table: same numbers as the aggregate
    SQL this used to run. Covers a table with no id column at all
    (fct_daily_revenue) and one where the id column is customer_id
    rather than order_id (raw_customers)."""
    assert run_basic_checks(table_name) == _direct_sql_report(table_name)


def test_run_basic_checks_over_mcp_matches_direct_sql_under_incident_4():
    """Incident 4 is the case the duplicate-transaction check exists
    for -- the same transaction re-emitted under brand-new order_ids --
    so it's the one where a discrepancy between the two routes would
    actually change an investigation's conclusion."""
    incident_04_duplicate_rows.apply()
    try:
        report = run_basic_checks("raw_orders")

        assert report == _direct_sql_report("raw_orders")
        assert report["duplicate_transaction_groups"] > 0
    finally:
        common.reset_to_clean_baseline()


def test_run_basic_checks_over_mcp_matches_direct_sql_under_incident_3():
    """Incident 3 renames raw_orders.created_at to order_created_at
    while the server is already running, which tests two things at once:
    that the timestamp-column fallback still picks the live column, and
    that the server reflects the schema per call instead of caching the
    one it saw at startup."""
    incident_03_schema_change.apply()
    try:
        schema = call_tool(POSTGRES_SERVER, "get_schema", {"table_name": "raw_orders"})
        column_names = [column["name"] for column in schema["columns"]]
        assert "order_created_at" in column_names
        assert "created_at" not in column_names

        assert run_basic_checks("raw_orders") == _direct_sql_report("raw_orders")
    finally:
        common.reset_to_clean_baseline()


def test_run_basic_checks_raises_for_a_table_outside_the_warehouse():
    """data_quality_node relies on this to skip names that aren't real
    tables (e.g. an unmaterialized mart)."""
    with pytest.raises(ValueError):
        run_basic_checks("mart_revenue_trends")


def test_retrieve_over_mcp_matches_the_in_process_retriever():
    ingest()

    query = "revenue is lower than expected for one day"
    over_mcp = call_tool(
        RETRIEVAL_SERVER, "retrieve", {"query": query, "n_results": 5}
    )

    in_process = retrieve(query, n_results=5)
    assert over_mcp["result_count"] == len(in_process)
    assert [hit["document"] for hit in over_mcp["results"]] == [
        hit["document"] for hit in in_process
    ]
    assert [hit["metadata"] for hit in over_mcp["results"]] == [
        hit["metadata"] for hit in in_process
    ]


def test_retrieve_over_mcp_honours_the_type_filter():
    ingest()

    result = call_tool(
        RETRIEVAL_SERVER,
        "retrieve",
        {"query": "daily revenue", "filter_type": "sql_model", "n_results": 2},
    )

    assert result["filter_type"] == "sql_model"
    assert result["result_count"] == 2
    assert all(hit["metadata"]["type"] == "sql_model" for hit in result["results"])


def test_calling_an_unknown_server_is_a_programming_error():
    with pytest.raises(KeyError):
        call_tool("no_such_server", "get_schema", {"table_name": "raw_orders"})
