"""Tests for the cyclic (Step 6) investigation LangGraph.

Forces the offline heuristic fallbacks (see app/graph/sql_review.py and
app/graph/root_cause.py) by clearing GOOGLE_API_KEY, so these tests
exercise the graph deterministically and without any network calls or
API cost.

That fallback is also what makes the retry loop testable for free. The
heuristic root-cause generator doesn't reason about the evidence, it
just restates the highest-scoring pieces of it, so validation_node's
direct re-check against the warehouse refuses to confirm what it
produces -- which is exactly the "hypothesis didn't hold up" path the
loop exists to handle, and it drives the graph all the way to the retry
cap and into human_review_node.
"""

from __future__ import annotations

from app.db.investigations import create_investigation, get_investigation
from app.db.models import InvestigationStatus
from app.graph.nodes import (
    MAX_RETRIES,
    RESOLVE_CONFIDENCE_THRESHOLD,
    _select_new_evidence,
    data_quality_node,
    human_review_node,
    lineage_agent_node,
    manager_node,
    sql_analysis_node,
)
from app.graph.root_cause import MAX_HYPOTHESES
from app.graph.workflow import route_after_validation, run_investigation
from app.retrieval.ingest import ingest

ALL_AGENTS = ["lineage_agent", "sql_analysis", "data_quality"]


def _hypothesis(confidence: float, description: str = "some claim") -> dict:
    return {
        "description": description,
        "supporting_evidence": ["lineage"],
        "confidence_score": confidence,
    }


def _validation(confirmed: bool, gap: str = "join_not_present") -> dict:
    return {
        "claim_kind": "join",
        "confirmed": confirmed,
        "checked": "re-read the SQL models",
        "note": "note",
        "gap": "" if confirmed else gap,
    }


def test_investigation_runs_the_full_cycle_and_persists(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ingest()

    final_state = run_investigation(
        "Total revenue for 2024-01-20 looks lower than expected."
    )

    assert final_state["investigation_id"] is not None

    sources = {item["source"] for item in final_state["evidence"]}
    assert {"lineage", "sql_analysis", "data_quality", "validation"} <= sources

    assert 1 <= len(final_state["hypotheses"]) <= MAX_HYPOTHESES * (MAX_RETRIES + 1)
    for hypothesis in final_state["hypotheses"]:
        assert 0.0 <= hypothesis["confidence_score"] <= 1.0
        assert hypothesis["supporting_evidence"]

    investigation = get_investigation(final_state["investigation_id"])
    assert investigation is not None
    assert investigation.status in (
        InvestigationStatus.RESOLVED,
        InvestigationStatus.NEEDS_HUMAN_REVIEW,
    )
    assert len(investigation.evidence) == len(final_state["evidence"])
    assert len(investigation.hypotheses) == len(final_state["hypotheses"])
    # human_review_node is terminal, so that's where the persisted
    # workflow position should have come to rest.
    assert investigation.workflow_state["current_node"] == "human_review"
    assert investigation.workflow_state["max_retries"] == MAX_RETRIES


def test_loop_retries_to_the_cap_when_the_hypothesis_is_never_confirmed(monkeypatch):
    """The offline heuristic can't produce a hypothesis the direct
    re-check will confirm, so this exercises the full retry budget and
    the refusal to claim a root cause at the end of it."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ingest()

    final_state = run_investigation("Revenue looks wrong for one day in January.")

    assert final_state["retry_count"] == MAX_RETRIES
    # One note per unconfirmed validation, which includes the final pass
    # that gave up and routed to human review rather than retrying again.
    assert len(final_state["validation_notes"]) == MAX_RETRIES + 1
    assert final_state["status"] == InvestigationStatus.NEEDS_HUMAN_REVIEW.value
    assert final_state["final_root_cause"] is None

    investigation = get_investigation(final_state["investigation_id"])
    assert investigation.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert investigation.final_root_cause is None
    assert investigation.workflow_state["retries_used"] == MAX_RETRIES


def test_no_evidence_is_recorded_twice_across_retry_passes(monkeypatch):
    """`evidence` uses an additive reducer, so a retry re-running an
    agent must not re-append what that agent already contributed."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ingest()

    final_state = run_investigation("Daily revenue dropped without explanation.")

    assert final_state["retry_count"] > 0, "expected this run to loop at least once"
    entries = [(item["source"], item["finding"]) for item in final_state["evidence"]]
    assert len(entries) == len(set(entries))

    investigation = get_investigation(final_state["investigation_id"])
    persisted = [(item["source"], item["finding"]) for item in investigation.evidence]
    assert len(persisted) == len(set(persisted))


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
    assert refetched.status in (
        InvestigationStatus.RESOLVED,
        InvestigationStatus.NEEDS_HUMAN_REVIEW,
    )
    assert len(refetched.hypotheses) >= 1


def test_manager_schedules_every_agent_on_the_first_pass():
    result = manager_node({"issue_description": "first pass", "investigation_id": None})

    assert result["agents_to_run"] == ALL_AGENTS
    assert result["retry_count"] == 0
    assert result["follow_up_query"] is None


def test_manager_targets_a_subset_of_agents_on_retry():
    """A retry should bring in a different kind of evidence, not re-run
    everything -- re-running all three would mostly re-derive findings
    already on file."""
    investigation = create_investigation("retry targeting")
    result = manager_node(
        {
            "investigation_id": str(investigation.id),
            "issue_description": "Revenue is undercounted",
            "agents_to_run": ALL_AGENTS,
            "agents_completed": [],
            "retry_count": 0,
            "validation": _validation(confirmed=False, gap="join_not_present"),
        }
    )

    assert result["retry_count"] == 1
    assert result["agents_to_run"] == ["lineage_agent", "data_quality"]
    assert "sql_analysis" not in result["agents_to_run"]
    assert set(result["agents_completed"]) == set(ALL_AGENTS)
    # Retrieval is refocused so the re-run surfaces new context rather
    # than the same top hits as the first pass.
    assert result["follow_up_query"] != "Revenue is undercounted"
    assert result["follow_up_query"].startswith("Revenue is undercounted")
    # Cleared so this pass is judged on its own hypothesis.
    assert result["validation"] is None


def test_specialist_nodes_no_op_when_this_pass_did_not_schedule_them():
    state = {
        "investigation_id": None,
        "issue_description": "anything",
        "agents_to_run": ["lineage_agent"],
        "evidence": [],
        "relevant_sql_models": [{"file_path": "x.sql", "table_name": "x", "sql_text": "SELECT 1"}],
        "relevant_tables": ["raw_orders"],
    }

    assert sql_analysis_node(state) == {}
    assert data_quality_node(state) == {}


def test_select_new_evidence_drops_entries_already_recorded():
    state = {
        "evidence": [
            {"source": "lineage", "finding": "already seen", "confidence": 0.5},
        ]
    }
    candidates = [
        {"source": "lineage", "finding": "already seen", "confidence": 0.9},
        {"source": "lineage", "finding": "brand new", "confidence": 0.4},
        {"source": "data_quality", "finding": "already seen", "confidence": 0.4},
    ]

    fresh = _select_new_evidence(state, candidates)

    # Same source + same finding is a duplicate regardless of the
    # confidence attached to it; the same text from a different source
    # is a genuinely different observation.
    assert [item["finding"] for item in fresh] == ["brand new", "already seen"]
    assert [item["source"] for item in fresh] == ["lineage", "data_quality"]


def test_route_after_validation_retries_when_the_recheck_contradicts():
    state = {
        "validation": _validation(confirmed=False),
        "top_hypothesis": _hypothesis(0.95),
        "retry_count": 0,
    }
    assert route_after_validation(state) == "manager"


def test_route_after_validation_retries_when_confidence_is_too_weak():
    state = {
        "validation": _validation(confirmed=True),
        "top_hypothesis": _hypothesis(0.4),
        "retry_count": 0,
    }
    assert route_after_validation(state) == "manager"


def test_route_after_validation_proceeds_when_confirmed_and_confident():
    state = {
        "validation": _validation(confirmed=True),
        "top_hypothesis": _hypothesis(0.9),
        "retry_count": 0,
    }
    assert route_after_validation(state) == "human_review"


def test_route_after_validation_stops_looping_once_retries_are_exhausted():
    state = {
        "validation": _validation(confirmed=False),
        "top_hypothesis": _hypothesis(0.95),
        "retry_count": MAX_RETRIES,
    }
    assert route_after_validation(state) == "human_review"


def test_human_review_resolves_a_confidently_supported_hypothesis():
    investigation = create_investigation("high confidence")
    result = human_review_node(
        {
            "investigation_id": str(investigation.id),
            "issue_description": "high confidence",
            "top_hypothesis": _hypothesis(0.92, "the join in 01_stg_orders_cleaned.sql"),
            "validation": _validation(confirmed=True),
            "retry_count": 0,
        }
    )

    assert result["status"] == InvestigationStatus.RESOLVED.value
    assert result["final_root_cause"] == "the join in 01_stg_orders_cleaned.sql"

    refetched = get_investigation(investigation.id)
    assert refetched.status == InvestigationStatus.RESOLVED
    assert refetched.final_root_cause == "the join in 01_stg_orders_cleaned.sql"


def test_human_review_flags_a_weakly_supported_hypothesis_without_asserting_a_cause():
    investigation = create_investigation("low confidence")
    result = human_review_node(
        {
            "investigation_id": str(investigation.id),
            "issue_description": "low confidence",
            "top_hypothesis": _hypothesis(RESOLVE_CONFIDENCE_THRESHOLD, "a guess"),
            "validation": _validation(confirmed=False),
            "retry_count": MAX_RETRIES,
        }
    )

    # Sitting exactly on the threshold is not "above" it.
    assert result["status"] == InvestigationStatus.NEEDS_HUMAN_REVIEW.value
    assert result["final_root_cause"] is None

    refetched = get_investigation(investigation.id)
    assert refetched.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert refetched.final_root_cause is None


def test_lineage_agent_node_tags_evidence_with_lineage_source(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ingest()

    investigation = create_investigation("revenue calculation region")
    state = {
        "investigation_id": str(investigation.id),
        "issue_description": "revenue calculation region",
        "agents_to_run": ALL_AGENTS,
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
        "agents_to_run": ALL_AGENTS,
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
        "agents_to_run": ALL_AGENTS,
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
        "agents_to_run": ALL_AGENTS,
        "evidence": [],
        "relevant_tables": ["raw_orders"],
    }
    result = data_quality_node(state)

    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["source"] == "data_quality"
    assert "raw_orders" in result["evidence"][0]["finding"]
