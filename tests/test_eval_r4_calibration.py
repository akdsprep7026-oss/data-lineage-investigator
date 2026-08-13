"""Unit tests for R4 fine buckets / threshold sims (no live LLM)."""

from __future__ import annotations

from tests.eval_confidence_threshold import LabeledRun
from tests.eval_r4_calibration import fine_buckets, simulate


def _run(confidence: float, *, correct: bool = True, confirmed: bool = True) -> LabeledRun:
    return LabeledRun(
        dataset="unit",
        benchmark_key="1",
        run=1,
        claim_source="llm",
        confidence=confidence,
        confirmed=confirmed,
        contradicted=False,
        unknown_claim=False,
        status="resolved" if confirmed and confidence >= 0.8 else "needs_human_review",
        claim_kind="join",
        artifact="sql_models/01_stg_orders_cleaned.sql",
        expected_claim_kind="join",
        label_correct=correct,
        root_token_ok=correct,
        structured_claim_ok=correct,
        structured_artifact_ok=correct,
    )


def test_fine_buckets_isolate_exactly_0_8() -> None:
    runs = [_run(0.75), _run(0.8), _run(0.82), _run(0.9)]
    by_name = {row["bucket"]: row for row in fine_buckets(runs)}
    assert by_name["(0.70,0.75]"]["count"] == 1
    assert by_name["exactly 0.80"]["count"] == 1
    assert by_name["(0.80,0.85]"]["count"] == 1
    assert by_name["(0.85,0.90]"]["count"] == 1


def test_inclusive_vs_exclusive_threshold() -> None:
    runs = [_run(0.8), _run(0.81)]
    ge = simulate(runs, ">=0.80", lambda c: c >= 0.80)
    gt = simulate(runs, ">0.80", lambda c: c > 0.80)
    assert ge["resolved_count"] == 2
    assert gt["resolved_count"] == 1
    assert ge["false_resolution_count"] == 0
