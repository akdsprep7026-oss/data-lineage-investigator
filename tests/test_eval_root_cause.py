"""Unit tests for root-cause accuracy eval metrics (no live LLM)."""

from __future__ import annotations

from tests.eval_root_cause import (
    AggregateMetrics,
    BenchmarkIncident,
    artifact_matches,
    consistency,
    load_benchmarks,
    root_cause_corresponds,
    score_run,
)


def _bench(
    kind: str = "stale_pipeline",
    artifacts: tuple[str, ...] = ("build_fct_daily_revenue",),
    tokens: tuple[str, ...] = ("build_fct_daily_revenue", "fail", "stale"),
) -> BenchmarkIncident:
    return BenchmarkIncident(
        key="2",
        title="Stale pipeline",
        issue_description="Revenue looks stale",
        ground_truth_root_cause="Job failed",
        expected_claim_kind=kind,
        expected_artifacts=artifacts,
        ground_truth_tokens=tokens,
    )


def test_load_benchmarks_matches_sandbox_kinds() -> None:
    benches = load_benchmarks()
    assert [b.key for b in benches] == ["1", "2", "3", "4"]
    assert [b.expected_claim_kind for b in benches] == [
        "join",
        "stale_pipeline",
        "schema_change",
        "duplicates",
    ]
    assert all(b.issue_description for b in benches)
    assert all(b.ground_truth_root_cause for b in benches)


def test_artifact_matches_path_and_stem() -> None:
    expected = ("sql_models/01_stg_orders_cleaned.sql", "stg_orders_cleaned")
    assert artifact_matches(
        expected,
        "app/sandbox_data/sql_models/01_stg_orders_cleaned.sql",
        "ignored",
    )
    assert artifact_matches(expected, None, "bug in stg_orders_cleaned join")
    assert not artifact_matches(expected, "build_fct_daily_revenue", "job failed")


def test_root_cause_corresponds_majority_tokens() -> None:
    tokens = ("build_fct_daily_revenue", "fail", "stale")
    assert root_cause_corresponds(
        tokens, "build_fct_daily_revenue failed leaving fct stale"
    )
    assert not root_cause_corresponds(tokens, "maybe a filter issue")


def test_score_end_to_end_correct() -> None:
    capture = {
        "top_hypothesis": {
            "description": "build_fct_daily_revenue failed leaving fct stale",
            "confidence_score": 0.9,
            "claim_kind": "stale_pipeline",
            "artifact": "build_fct_daily_revenue",
        },
        "resolve_claim_kind": "stale_pipeline",
        "validation": {"confirmed": True, "gap": None},
        "status": "resolved",
        "final_root_cause": "build_fct_daily_revenue failed; fct is stale",
    }
    score = score_run(_bench(), capture)
    assert score["end_to_end_correct"] is True
    assert score["structured_claim_kind_ok"] is True
    assert score["structured_artifact_ok"] is True
    assert score["false_resolution"] is False
    assert score["failure_mode"] is None


def test_score_false_resolution_wrong_kind() -> None:
    capture = {
        "top_hypothesis": {
            "description": "INNER JOIN drops rows",
            "confidence_score": 0.95,
            "claim_kind": "join",
            "artifact": "sql_models/01_stg_orders_cleaned.sql",
        },
        "resolve_claim_kind": "join",
        "validation": {"confirmed": True, "gap": None},
        "status": "resolved",
        "final_root_cause": "INNER JOIN drops rows",
    }
    score = score_run(_bench(), capture)
    assert score["end_to_end_correct"] is False
    assert score["false_resolution"] is True
    assert score["failure_mode"] == "wrong_claim_kind"


def test_score_unknown_does_not_resolve_safely() -> None:
    capture = {
        "top_hypothesis": {
            "description": "unclear",
            "confidence_score": 0.95,
            "claim_kind": "unknown",
            "artifact": None,
        },
        "resolve_claim_kind": "unknown",
        "validation": {"confirmed": False, "gap": "unknown"},
        "status": "needs_human_review",
        "final_root_cause": None,
    }
    score = score_run(_bench(), capture)
    assert score["resolved"] is False
    assert score["unknown_resolved"] is False
    assert score["human_review"] is True
    assert score["failure_mode"] == "unknown_unclassifiable"


def test_structured_unknown_with_keyword_fallback_is_unknown_mode() -> None:
    """Structured unknown is the R1 primary signal even if keywords say join."""
    capture = {
        "top_hypothesis": {
            "description": "INNER JOIN mentioned in dumped SQL comments",
            "confidence_score": 0.6,
            "claim_kind": "unknown",
            "artifact": None,
        },
        "resolve_claim_kind": "join",
        "validation": {"confirmed": False, "gap": "join_not_present"},
        "status": "needs_human_review",
        "final_root_cause": None,
    }
    score = score_run(_bench(), capture)
    assert score["structured_claim_kind_ok"] is False
    assert score["fallback_claim_kind_ok"] is False
    assert score["failure_mode"] == "unknown_unclassifiable"
    assert score["resolved_kind"] == "join"


def test_score_contradicted_does_not_resolve() -> None:
    capture = {
        "top_hypothesis": {
            "description": "schema rename",
            "confidence_score": 0.9,
            "claim_kind": "schema_change",
            "artifact": "created_at",
        },
        "resolve_claim_kind": "schema_change",
        "validation": {"confirmed": False, "gap": "contradiction"},
        "status": "needs_human_review",
        "final_root_cause": None,
    }
    score = score_run(
        _bench(
            kind="stale_pipeline",
            artifacts=("build_fct_daily_revenue",),
            tokens=("build_fct_daily_revenue", "fail", "stale"),
        ),
        capture,
    )
    assert score["contradicted_resolved"] is False
    assert score["confidence_only_resolved"] is False


def test_aggregate_and_consistency() -> None:
    agg = AggregateMetrics()
    good = score_run(
        _bench(),
        {
            "top_hypothesis": {
                "description": "build_fct_daily_revenue failed stale",
                "confidence_score": 0.9,
                "claim_kind": "stale_pipeline",
                "artifact": "build_fct_daily_revenue",
            },
            "resolve_claim_kind": "stale_pipeline",
            "validation": {"confirmed": True},
            "status": "resolved",
            "final_root_cause": "build_fct_daily_revenue failed stale",
        },
    )
    bad = score_run(
        _bench(),
        {
            "top_hypothesis": {
                "description": "duplicates",
                "confidence_score": 0.7,
                "claim_kind": "duplicates",
                "artifact": "raw_orders",
            },
            "resolve_claim_kind": "duplicates",
            "validation": {"confirmed": False},
            "status": "needs_human_review",
            "final_root_cause": None,
        },
    )
    agg.add(good)
    agg.add(bad)
    rates = agg.rates()
    assert rates["n"] == 2
    assert rates["claim_kind_accuracy"] == 0.5
    assert rates["resolution_rate"] == 0.5
    assert rates["false_resolution_rate"] == 0.0
    assert rates["end_to_end_correctness"] == 0.5
    assert consistency(["stale_pipeline", "duplicates", "stale_pipeline"]) == (2, 3)
