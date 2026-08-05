"""Tests for the Step 5 linear investigation LangGraph.

Forces the offline heuristic fallbacks (see app/graph/sql_review.py and
app/graph/root_cause.py) by clearing GOOGLE_API_KEY, so these tests
exercise the graph deterministically and without any network calls or
API cost: manager -> lineage_agent -> sql_analysis -> data_quality ->
root_cause, wired sequentially with no loops.
"""

from __future__ import annotations

from app.db.investigations import create_investigation, get_investigation
from app.db.models import InvestigationStatus
from app.graph.nodes import data_quality_node, lineage_agent_node, sql_analysis_node
from app.graph.root_cause import MAX_HYPOTHESES
from app.graph.workflow import run_investigation
from app.retrieval.ingest import ingest


def test_investigation_runs_through_all_five_nodes_and_persists(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ingest()

    final_state = run_investigation(
        "Total revenue for 2024-01-20 looks lower than expected."
    )

    assert final_state["investigation_id"] is not None
    assert final_state["status"] == InvestigationStatus.INVESTIGATING.value

    sources = {item["source"] for item in final_state["evidence"]}
    assert "lineage" in sources
    assert "sql_analysis" in sources
    assert "data_quality" in sources

    assert 1 <= len(final_state["hypotheses"]) <= MAX_HYPOTHESES
    for hypothesis in final_state["hypotheses"]:
        assert 0.0 <= hypothesis["confidence_score"] <= 1.0
        assert hypothesis["supporting_evidence"]

    investigation = get_investigation(final_state["investigation_id"])
    assert investigation is not None
    assert investigation.status == InvestigationStatus.INVESTIGATING
    assert len(investigation.evidence) == len(final_state["evidence"])
    assert len(investigation.hypotheses) == len(final_state["hypotheses"])
    # No loop/validation step yet -- root_cause_node doesn't set this.
    assert investigation.final_root_cause is None


def test_investigation_can_resume_an_existing_investigation_id(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ingest()

    pre_created = create_investigation("Dashboard shows no data for 2024-01-30")

    final_state = run_investigation(
        pre_created.issue_description,
        investigation_id=str(pre_created.id),
    )

    assert final_state["investigation_id"] == str(pre_created.id)
    refetched = get_investigation(pre_created.id)
    assert refetched.status == InvestigationStatus.INVESTIGATING
    assert len(refetched.hypotheses) >= 1


def test_lineage_agent_node_tags_evidence_with_lineage_source(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ingest()

    investigation = create_investigation("revenue calculation region")
    state = {
        "investigation_id": str(investigation.id),
        "issue_description": "revenue calculation region",
        "evidence": [],
    }
    result = lineage_agent_node(state)

    assert result["evidence"]
    assert all(item["source"] == "lineage" for item in result["evidence"])
    assert result["relevant_sql_models"]
    assert result["relevant_tables"]


def test_sql_analysis_node_reviews_each_relevant_sql_model_and_flags_inner_join(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    investigation = create_investigation("test issue")
    state = {
        "investigation_id": str(investigation.id),
        "issue_description": "test issue",
        "evidence": [],
        "relevant_sql_models": [
            {
                "file_path": "app/sandbox_data/sql_models/01_stg_orders_cleaned.sql",
                "table_name": "stg_orders_cleaned",
                "sql_text": (
                    "SELECT * FROM raw_orders o INNER JOIN raw_customers c "
                    "ON o.customer_id = c.customer_id"
                ),
            }
        ],
    }
    result = sql_analysis_node(state)

    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["source"] == "sql_analysis"
    assert "INNER JOIN" in result["evidence"][0]["finding"]


def test_data_quality_node_skips_tables_not_in_the_sandbox_warehouse(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    investigation = create_investigation("test issue")
    state = {
        "investigation_id": str(investigation.id),
        "issue_description": "test issue",
        "evidence": [],
        "relevant_tables": ["not_a_real_table"],
    }
    result = data_quality_node(state)

    assert result["evidence"] == []


def test_data_quality_node_checks_a_real_table(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    investigation = create_investigation("test issue")
    state = {
        "investigation_id": str(investigation.id),
        "issue_description": "test issue",
        "evidence": [],
        "relevant_tables": ["raw_orders"],
    }
    result = data_quality_node(state)

    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["source"] == "data_quality"
    assert "raw_orders" in result["evidence"][0]["finding"]
