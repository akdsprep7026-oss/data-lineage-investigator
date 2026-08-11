"""Tests for the cyclic (Step 6) investigation LangGraph.

These run against the offline heuristic fallbacks (see
app/graph/sql_review.py and app/graph/root_cause.py), which tests/
conftest.py enforces suite-wide by clearing every provider API key, so
they're deterministic and cost no network calls or API quota.

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
    MAX_VALIDATION_PASSES,
    RESOLVE_CONFIDENCE_THRESHOLD,
    _select_agents_for_issue,
    _select_new_evidence,
    data_quality_node,
    etl_agent_node,
    human_review_node,
    lineage_agent_node,
    manager_node,
    schema_agent_node,
    sql_analysis_node,
)
from app.graph.root_cause import MAX_HYPOTHESES
from app.graph.workflow import route_after_validation, run_investigation
from app.retrieval.ingest import ingest
from app.sandbox_data.incidents import common, incident_02_stale_pipeline, incident_03_schema_change

ALL_AGENTS = ["lineage_agent", "sql_analysis", "data_quality", "etl_agent", "schema_agent"]


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


def test_investigation_runs_the_full_cycle_and_persists():
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


def test_loop_retries_to_the_cap_when_the_hypothesis_is_never_confirmed():
    """The offline heuristic can't produce a hypothesis the direct
    re-check will confirm, so this exercises the full retry budget and
    the refusal to claim a root cause at the end of it."""
    ingest()

    final_state = run_investigation("Revenue looks wrong for one day in January.")

    assert final_state["retry_count"] == MAX_RETRIES
    assert final_state["validation_pass_count"] == MAX_VALIDATION_PASSES
    # One note per unconfirmed validation, which includes the final pass
    # that gave up and routed to human review rather than retrying again.
    assert len(final_state["validation_notes"]) == MAX_RETRIES + 1
    assert final_state["status"] == InvestigationStatus.NEEDS_HUMAN_REVIEW.value
    assert final_state["final_root_cause"] is None

    investigation = get_investigation(final_state["investigation_id"])
    assert investigation.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert investigation.final_root_cause is None
    assert investigation.workflow_state["retries_used"] == MAX_RETRIES
    assert investigation.workflow_state["validation_pass_count"] == MAX_VALIDATION_PASSES
    system_notes = [
        item for item in investigation.evidence if item["source"] == "system"
    ]
    assert system_notes
    assert "could not establish a sufficiently verified root cause" in system_notes[0]["finding"]


def test_no_evidence_is_recorded_twice_across_retry_passes():
    """`evidence` uses an additive reducer, so a retry re-running an
    agent must not re-append what that agent already contributed."""
    ingest()

    final_state = run_investigation("Daily revenue dropped without explanation.")

    assert final_state["retry_count"] > 0, "expected this run to loop at least once"
    entries = [(item["source"], item["finding"]) for item in final_state["evidence"]]
    assert len(entries) == len(set(entries))

    investigation = get_investigation(final_state["investigation_id"])
    persisted = [(item["source"], item["finding"]) for item in investigation.evidence]
    assert len(persisted) == len(set(persisted))


def test_investigation_can_resume_an_existing_investigation_id():
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
    everything -- re-running every specialist would mostly re-derive
    findings already on file."""
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


def test_manager_retargets_to_etl_agent_when_the_pipeline_gap_is_job_specific():
    """A gap that means "which job is at fault" should route to
    etl_agent (which reads job metadata directly), not data_quality."""
    investigation = create_investigation("retry targeting etl")
    result = manager_node(
        {
            "investigation_id": str(investigation.id),
            "issue_description": "Dashboard is stale",
            "agents_to_run": ALL_AGENTS,
            "agents_completed": [],
            "retry_count": 0,
            "validation": _validation(confirmed=False, gap="pipeline_job_unnamed"),
        }
    )

    assert result["agents_to_run"] == ["lineage_agent", "etl_agent"]


def test_manager_retargets_to_schema_agent_when_the_schema_gap_is_model_unnamed():
    """A gap that means "which model references the missing column"
    should route to schema_agent (which checks column references
    directly), not sql_analysis."""
    investigation = create_investigation("retry targeting schema")
    result = manager_node(
        {
            "investigation_id": str(investigation.id),
            "issue_description": "Job is failing with a database error",
            "agents_to_run": ALL_AGENTS,
            "agents_completed": [],
            "retry_count": 0,
            "validation": _validation(confirmed=False, gap="schema_model_unnamed"),
        }
    )

    assert result["agents_to_run"] == ["lineage_agent", "schema_agent"]


def test_select_agents_for_issue_prioritizes_lineage_sql_and_data_quality_for_missing_rows():
    selected = _select_agents_for_issue(
        "Revenue looks lower than expected -- some rows seem to be missing."
    )
    assert selected == ["lineage_agent", "sql_analysis", "data_quality"]


def test_select_agents_for_issue_prioritizes_etl_agent_for_staleness_language():
    selected = _select_agents_for_issue(
        "The dashboard shows no data for the last two days; the job seems delayed."
    )
    assert selected == ["lineage_agent", "etl_agent"]


def test_select_agents_for_issue_prioritizes_schema_agent_for_schema_language():
    selected = _select_agents_for_issue(
        "The job is failing on every run with a database error referencing a renamed column."
    )
    assert set(selected) == {"lineage_agent", "etl_agent", "schema_agent"}


def test_select_agents_for_issue_falls_back_to_everything_when_nothing_matches():
    selected = _select_agents_for_issue("Something seems off with the numbers.")
    assert selected == ALL_AGENTS


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
    assert etl_agent_node(state) == {}
    assert schema_agent_node(state) == {}


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
        "validation_pass_count": 1,
    }
    assert route_after_validation(state) == "manager"


def test_route_after_validation_retries_when_confidence_is_too_weak():
    state = {
        "validation": _validation(confirmed=True),
        "top_hypothesis": _hypothesis(0.4),
        "retry_count": 0,
        "validation_pass_count": 1,
    }
    assert route_after_validation(state) == "manager"


def test_route_after_validation_proceeds_when_confirmed_and_confident():
    state = {
        "validation": _validation(confirmed=True),
        "top_hypothesis": _hypothesis(0.9),
        "retry_count": 0,
        "validation_pass_count": 1,
    }
    assert route_after_validation(state) == "human_review"


def test_route_after_validation_stops_looping_once_retries_are_exhausted():
    state = {
        "validation": _validation(confirmed=False),
        "top_hypothesis": _hypothesis(0.95),
        "retry_count": MAX_RETRIES,
        "validation_pass_count": 1,
    }
    assert route_after_validation(state) == "human_review"


def test_route_after_validation_stops_when_validation_pass_cap_is_hit():
    """Even if retry_count is somehow stuck at 0, the validation pass
    cap must still terminate the loop."""
    state = {
        "validation": _validation(confirmed=False),
        "top_hypothesis": _hypothesis(0.95),
        "retry_count": 0,
        "validation_pass_count": MAX_VALIDATION_PASSES,
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
            "validation_pass_count": 1,
            "evidence": [],
        }
    )

    assert result["status"] == InvestigationStatus.RESOLVED.value
    assert result["final_root_cause"] == "the join in 01_stg_orders_cleaned.sql"

    refetched = get_investigation(investigation.id)
    assert refetched.status == InvestigationStatus.RESOLVED
    assert refetched.final_root_cause == "the join in 01_stg_orders_cleaned.sql"


def test_human_review_does_not_resolve_high_confidence_without_confirmation():
    """Confidence alone is not enough -- validation must confirm."""
    investigation = create_investigation("confident but unconfirmed")
    result = human_review_node(
        {
            "investigation_id": str(investigation.id),
            "issue_description": "confident but unconfirmed",
            "top_hypothesis": _hypothesis(0.95, "a specific-sounding guess"),
            "validation": _validation(confirmed=False),
            "retry_count": MAX_RETRIES,
            "validation_pass_count": MAX_VALIDATION_PASSES,
            "evidence": [],
        }
    )

    assert result["status"] == InvestigationStatus.NEEDS_HUMAN_REVIEW.value
    assert result["final_root_cause"] is None

    refetched = get_investigation(investigation.id)
    assert refetched.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert any(item["source"] == "system" for item in refetched.evidence)
    assert refetched.workflow_state["review_reason"]


def test_human_review_flags_a_weakly_supported_hypothesis_without_asserting_a_cause():
    investigation = create_investigation("low confidence")
    result = human_review_node(
        {
            "investigation_id": str(investigation.id),
            "issue_description": "low confidence",
            "top_hypothesis": _hypothesis(RESOLVE_CONFIDENCE_THRESHOLD, "a guess"),
            "validation": _validation(confirmed=False),
            "retry_count": MAX_RETRIES,
            "validation_pass_count": MAX_VALIDATION_PASSES,
            "evidence": [],
        }
    )

    # Sitting exactly on the threshold is not "above" it.
    assert result["status"] == InvestigationStatus.NEEDS_HUMAN_REVIEW.value
    assert result["final_root_cause"] is None

    refetched = get_investigation(investigation.id)
    assert refetched.status == InvestigationStatus.NEEDS_HUMAN_REVIEW
    assert refetched.final_root_cause is None


def test_lineage_agent_node_tags_evidence_with_lineage_source():
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


def test_sql_analysis_node_reviews_each_relevant_sql_model_and_flags_inner_join():
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


def test_data_quality_node_skips_tables_not_in_the_sandbox_warehouse():
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


def test_data_quality_node_checks_a_real_table():
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


def test_data_quality_node_flags_duplicate_transactions_under_new_ids():
    """Incident 4's scenario: ~15 completed orders are re-emitted with
    brand-new order_ids for what's really the same transaction, so a
    same-id duplicate count alone (0 in this case, each new id appears
    exactly once) would miss it entirely. The customer/amount/day check
    should catch it instead."""
    from app.sandbox_data.incidents import incident_04_duplicate_rows

    incident_04_duplicate_rows.apply()
    try:
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
        finding = result["evidence"][0]["finding"]
        assert "same transaction re-emitted under a new id" in finding
        assert result["evidence"][0]["confidence"] >= 0.75
    finally:
        common.reset_to_clean_baseline()


def test_etl_agent_node_flags_a_failed_job():
    """Incident 2's scenario: build_fct_daily_revenue has been marked
    failed in pipeline_jobs.json. etl_agent_node should surface that
    directly, with high confidence, rather than needing to infer it from
    the data."""
    incident_02_stale_pipeline.apply()
    try:
        investigation = create_investigation("test issue")
        state = {
            "investigation_id": str(investigation.id),
            "issue_description": "test issue",
            "agents_to_run": ALL_AGENTS,
            "evidence": [],
            "relevant_tables": ["fct_daily_revenue"],
        }
        result = etl_agent_node(state)

        assert result["evidence"]
        assert all(item["source"] == "etl_agent" for item in result["evidence"])
        failed = [item for item in result["evidence"] if "build_fct_daily_revenue" in item["finding"]]
        assert failed
        assert "failed" in failed[0]["finding"]
        assert failed[0]["confidence"] > 0.5
    finally:
        common.reset_to_clean_baseline()


def test_etl_agent_node_reports_healthy_jobs_with_low_confidence():
    investigation = create_investigation("test issue")
    state = {
        "investigation_id": str(investigation.id),
        "issue_description": "test issue",
        "agents_to_run": ALL_AGENTS,
        "evidence": [],
        "relevant_tables": ["fct_daily_revenue"],
    }
    result = etl_agent_node(state)

    assert result["evidence"]
    assert all(item["confidence"] < 0.5 for item in result["evidence"])


def test_schema_agent_node_flags_a_column_that_no_longer_exists():
    """Incident 3's scenario: raw_orders.created_at was renamed to
    order_created_at, but the SQL model still references o.created_at.
    schema_agent_node should catch that by comparing against the live
    schema directly, without needing to execute anything."""
    incident_03_schema_change.apply()
    try:
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
                    "sql_text": common.CLEAN_STG_ORDERS_CLEANED_SQL,
                }
            ],
        }
        result = schema_agent_node(state)

        assert len(result["evidence"]) == 1
        assert result["evidence"][0]["source"] == "schema_agent"
        assert "created_at" in result["evidence"][0]["finding"]
        assert result["evidence"][0]["confidence"] > 0.5
    finally:
        common.reset_to_clean_baseline()


def test_schema_agent_node_reports_no_mismatch_when_columns_line_up():
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
                "sql_text": common.CLEAN_STG_ORDERS_CLEANED_SQL,
            }
        ],
    }
    result = schema_agent_node(state)

    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["source"] == "schema_agent"
    assert result["evidence"][0]["confidence"] < 0.5
