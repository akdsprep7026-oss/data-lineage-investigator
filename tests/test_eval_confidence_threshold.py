"""Unit tests for R3 confidence-threshold simulation (no live LLM)."""

from __future__ import annotations

from tests.eval_confidence_threshold import (
    LabeledRun,
    bucket_metrics,
    confirmation_splits,
    simulate_threshold,
)


def _run(
    *,
    confidence: float,
    confirmed: bool = True,
    label_correct: bool = True,
    claim_source: str = "llm",
    status: str = "needs_human_review",
    contradicted: bool = False,
    unknown_claim: bool = False,
    key: str = "1",
) -> LabeledRun:
    return LabeledRun(
        dataset="unit.json",
        benchmark_key=key,
        run=1,
        claim_source=claim_source,
        confidence=confidence,
        confirmed=confirmed,
        contradicted=contradicted,
        unknown_claim=unknown_claim,
        status=status,
        claim_kind="join",
        artifact="sql_models/01_stg_orders_cleaned.sql",
        expected_claim_kind="join",
        label_correct=label_correct,
        structured_claim_ok=label_correct,
        structured_artifact_ok=label_correct,
        root_token_ok=label_correct,
    )


def test_bucket_metrics_split_on_boundaries() -> None:
    runs = [
        _run(confidence=0.6),
        _run(confidence=0.7),
        _run(confidence=0.8),
        _run(confidence=0.85, status="resolved"),
        _run(confidence=0.95, status="resolved"),
    ]
    buckets = {row["bucket"]: row for row in bucket_metrics(runs)}
    assert buckets["A <=0.60"]["count"] == 1
    assert buckets["B (0.60,0.70]"]["count"] == 1
    assert buckets["C (0.70,0.80]"]["count"] == 1
    assert buckets["D (0.80,0.90]"]["count"] == 1
    assert buckets["E >0.90"]["count"] == 1
    assert buckets["D (0.80,0.90]"]["resolved_current"] == 1
    # Inclusive production gate: confirmed + exact 0.80 resolves.
    assert buckets["C (0.70,0.80]"]["resolved_current"] == 1


def test_simulate_threshold_false_resolution() -> None:
    runs = [
        _run(confidence=0.9, label_correct=True, status="resolved"),
        _run(confidence=0.9, label_correct=False, key="2", status="resolved"),
        _run(confidence=0.75, label_correct=True),
    ]
    sim = simulate_threshold(runs, 0.8)
    assert sim["resolved_count"] == 2
    assert sim["true_resolution_count"] == 1
    assert sim["false_resolution_count"] == 1
    assert sim["false_resolution_rate"] == 1 / 3

    sim_low = simulate_threshold(runs, 0.70)
    assert sim_low["resolved_count"] == 3
    assert sim_low["false_resolution_count"] == 1


def test_confirmation_splits_count_confirmed_low_confidence() -> None:
    runs = [
        _run(confidence=0.75, confirmed=True, label_correct=True),
        _run(confidence=0.8, confirmed=True, label_correct=True),
        _run(confidence=0.85, confirmed=True, label_correct=True),
        _run(confidence=0.9, confirmed=False, contradicted=True, label_correct=False),
    ]
    splits = confirmation_splits(runs)
    assert splits["confirmed_lt_0_8"]["count"] == 1
    assert splits["confirmed_lt_0_8"]["correct"] == 1
    assert splits["confirmed_gte_0_8"]["count"] == 2
    assert splits["contradicted_gte_0_8"]["count"] == 1
    assert splits["unconfirmed_gte_0_8"]["count"] == 1
