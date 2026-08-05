"""Tests for the direct re-checks behind validation_node.

These are the tests that matter most for the loop being worth having:
they pin down that a claim is confirmed only when the warehouse itself
agrees with it, and that a confident-sounding claim aimed at the wrong
artifact is rejected rather than waved through.

Each test applies a real incident scenario to the sandbox warehouse
(every incident's apply() resets to the clean baseline first, so they
don't contaminate each other) and the module resets the sandbox when
it's finished.
"""

from __future__ import annotations

import pytest

from app.graph.validation import classify_claim, validate_hypothesis
from app.sandbox_data.incidents import (
    common,
    incident_01_join_bug,
    incident_02_stale_pipeline,
    incident_03_schema_change,
    incident_04_duplicate_rows,
)


@pytest.fixture(scope="module", autouse=True)
def clean_sandbox_afterwards():
    yield
    common.reset_to_clean_baseline()


def hypothesis(description: str, confidence: float = 0.9) -> dict:
    return {
        "description": description,
        "supporting_evidence": [],
        "confidence_score": confidence,
    }


@pytest.mark.parametrize(
    "description, expected",
    [
        ("An INNER JOIN drops unmatched orders", "join"),
        ("The build_fct_daily_revenue job keeps failing so the table is stale", "stale_pipeline"),
        ("A column was renamed upstream so the model errors", "schema_change"),
        ("Orders were counted twice, inflating the total", "duplicates"),
        ("Someone restyled the dashboard legend", "unknown"),
    ],
)
def test_classify_claim(description, expected):
    assert classify_claim(description) == expected


def test_a_claim_that_names_no_checkable_failure_mode_is_not_confirmed():
    outcome = validate_hypothesis(hypothesis("Someone restyled the dashboard legend"))

    assert outcome["confirmed"] is False
    assert outcome["gap"] == "unclassifiable_claim"


def test_a_missing_hypothesis_is_reported_rather_than_assumed_true():
    outcome = validate_hypothesis(None)

    assert outcome["confirmed"] is False
    assert outcome["gap"] == "no_hypothesis"


def test_join_claim_is_confirmed_against_the_join_bug_incident():
    incident_01_join_bug.apply()

    outcome = validate_hypothesis(
        hypothesis(
            "sql_models/01_stg_orders_cleaned.sql uses an INNER JOIN against "
            "raw_customers, dropping orders from brand-new customers."
        )
    )

    assert outcome["claim_kind"] == "join"
    assert outcome["confirmed"] is True
    assert "01_stg_orders_cleaned.sql" in outcome["note"]


def test_join_claim_pointed_at_the_wrong_model_is_contradicted():
    """The kind of bug is right and the confidence is high, but the file
    blamed isn't the one at fault -- which is exactly the failure the
    re-check exists to catch."""
    incident_01_join_bug.apply()

    outcome = validate_hypothesis(
        hypothesis("The INNER JOIN in 02_fct_daily_revenue.sql is dropping rows.")
    )

    assert outcome["confirmed"] is False
    assert outcome["gap"] == "join_wrong_model"


def test_join_claim_is_contradicted_on_the_clean_baseline():
    common.reset_to_clean_baseline()

    outcome = validate_hypothesis(
        hypothesis("An INNER JOIN in the staging model is dropping orders.")
    )

    assert outcome["confirmed"] is False
    assert outcome["gap"] == "join_not_present"


def test_stale_pipeline_claim_is_confirmed_against_the_stale_pipeline_incident():
    incident_02_stale_pipeline.apply()

    outcome = validate_hypothesis(
        hypothesis(
            "The build_fct_daily_revenue job has been failing, so "
            "fct_daily_revenue was never refreshed."
        )
    )

    assert outcome["claim_kind"] == "stale_pipeline"
    assert outcome["confirmed"] is True
    assert "build_fct_daily_revenue" in outcome["note"]


def test_stale_pipeline_claim_naming_a_healthy_job_is_contradicted():
    incident_02_stale_pipeline.apply()

    outcome = validate_hypothesis(
        hypothesis(
            "The build_stg_orders_cleaned job has been failing, leaving stale data."
        )
    )

    assert outcome["confirmed"] is False
    assert outcome["gap"] == "pipeline_wrong_job"


def test_schema_change_claim_is_confirmed_by_re_executing_the_models():
    incident_03_schema_change.apply()

    outcome = validate_hypothesis(
        hypothesis(
            "raw_orders.created_at was renamed to order_created_at but "
            "01_stg_orders_cleaned.sql still references the old column."
        )
    )

    assert outcome["claim_kind"] == "schema_change"
    assert outcome["confirmed"] is True
    # Confirmation comes from the database rejecting the model, not from
    # anything the agents said about it.
    assert "no such column: o.created_at" in outcome["note"]


def test_schema_change_claim_is_contradicted_when_every_model_still_runs():
    common.reset_to_clean_baseline()

    outcome = validate_hypothesis(
        hypothesis("A column was renamed upstream and the model can no longer find it.")
    )

    assert outcome["confirmed"] is False
    assert outcome["gap"] == "schema_intact"


def test_duplicate_claim_is_confirmed_against_the_duplicate_rows_incident():
    incident_04_duplicate_rows.apply()

    outcome = validate_hypothesis(
        hypothesis("Some orders were counted twice, inflating that day's revenue.")
    )

    assert outcome["claim_kind"] == "duplicates"
    assert outcome["confirmed"] is True
    assert "different order_ids" in outcome["note"]


def test_duplicate_claim_is_contradicted_on_the_clean_baseline():
    """The baseline deliberately contains repeated order_ids, but the
    staging model already de-duplicates those, so they must not be
    mistaken for the bug."""
    common.reset_to_clean_baseline()

    outcome = validate_hypothesis(
        hypothesis("Orders were duplicated and counted twice.")
    )

    assert outcome["confirmed"] is False
    assert outcome["gap"] == "duplicates_absent"
