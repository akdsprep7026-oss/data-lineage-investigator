"""R4 LLM-only confidence calibration analysis (simulation only).

Reads an R4 campaign JSON from ``tests.eval_root_cause`` and computes
fine-grained confidence buckets plus inclusive/exclusive threshold sims.
Fallback runs are reported separately and excluded from LLM-only metrics.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.eval_confidence_threshold import (  # noqa: E402
    LabeledRun,
    has_root_token_overlap,
    infer_claim_source,
    is_label_correct,
    load_labeled_runs,
)
from tests.eval_root_cause import load_benchmarks, score_run  # noqa: E402

FINE_BUCKETS: tuple[tuple[str, Callable[[float], bool]], ...] = (
    ("<=0.60", lambda c: c <= 0.60),
    ("(0.60,0.70]", lambda c: 0.60 < c <= 0.70),
    ("(0.70,0.75]", lambda c: 0.70 < c <= 0.75),
    ("(0.75,0.80]", lambda c: 0.75 < c < 0.80),
    ("exactly 0.80", lambda c: abs(c - 0.80) < 1e-9),
    ("(0.80,0.85]", lambda c: 0.80 < c <= 0.85),
    ("(0.85,0.90]", lambda c: 0.85 < c <= 0.90),
    (">0.90", lambda c: c > 0.90),
)

# Note: (0.75,0.80] in the user prompt overlaps exactly 0.80; we split
# exactly 0.80 into its own bucket and use (0.75,0.80) open on the right
# so counts are disjoint and sum to n.

THRESHOLDS: tuple[tuple[str, Callable[[float], bool]], ...] = (
    (">=0.70", lambda c: c >= 0.70),
    (">0.70", lambda c: c > 0.70),
    (">=0.75", lambda c: c >= 0.75),
    (">0.75", lambda c: c > 0.75),
    (">=0.80", lambda c: c >= 0.80),
    (">0.80", lambda c: c > 0.80),
    (">=0.85", lambda c: c >= 0.85),
    (">0.85", lambda c: c > 0.85),
    (">=0.90", lambda c: c >= 0.90),
)


def fine_buckets(runs: list[LabeledRun]) -> list[dict[str, Any]]:
    rows = []
    for name, pred in FINE_BUCKETS:
        members = [run for run in runs if pred(run.confidence)]
        rows.append(
            {
                "bucket": name,
                "count": len(members),
                "confirmed": sum(1 for run in members if run.confirmed),
                "correct": sum(1 for run in members if run.label_correct),
                "incorrect": sum(1 for run in members if not run.label_correct),
                "resolved_at_gte_0_8": sum(
                    1
                    for run in members
                    if run.confirmed and run.confidence >= 0.8
                ),
                "human_review": sum(
                    1 for run in members if run.status == "needs_human_review"
                ),
                "false_at_gte_0_8": sum(
                    1
                    for run in members
                    if run.confirmed
                    and run.confidence >= 0.8
                    and not run.label_correct
                ),
                "p_correct": (
                    sum(1 for run in members if run.label_correct) / len(members)
                    if members
                    else None
                ),
            }
        )
    return rows


def simulate(runs: list[LabeledRun], name: str, pred: Callable[[float], bool]) -> dict[str, Any]:
    n = max(len(runs), 1)
    resolved = [run for run in runs if run.confirmed and pred(run.confidence)]
    true_res = [
        run
        for run in resolved
        if run.label_correct and run.root_token_ok
    ]
    # Primary false-resolution = resolved with wrong claim contract.
    false_res = [run for run in resolved if not run.label_correct]
    # Secondary: resolved with correct kind/artifact but weak root prose.
    weak_root = [
        run for run in resolved if run.label_correct and not run.root_token_ok
    ]
    human = [run for run in runs if not (run.confirmed and pred(run.confidence))]
    return {
        "threshold": name,
        "n": len(runs),
        "resolution_rate": len(resolved) / n,
        "true_resolution_rate": len(true_res) / n,
        "claim_correct_resolution_rate": sum(1 for run in resolved if run.label_correct) / n,
        "false_resolution_rate": len(false_res) / n,
        "human_review_rate": len(human) / n,
        "resolved_count": len(resolved),
        "true_resolution_count": len(true_res),
        "claim_correct_resolution_count": sum(1 for run in resolved if run.label_correct),
        "false_resolution_count": len(false_res),
        "weak_root_resolution_count": len(weak_root),
        "false_resolution_cases": [
            {
                "incident": run.benchmark_key,
                "run": run.run,
                "claim_kind": run.claim_kind,
                "expected": run.expected_claim_kind,
                "confidence": run.confidence,
            }
            for run in false_res
        ],
    }


def confirmation_quality(runs: list[LabeledRun]) -> dict[str, Any]:
    def count(pred: Callable[[LabeledRun], bool]) -> int:
        return sum(1 for run in runs if pred(run))

    return {
        "confirmed_correct_le_0_8": count(
            lambda r: r.confirmed and r.label_correct and r.confidence <= 0.8
        ),
        "confirmed_correct_gte_0_8": count(
            lambda r: r.confirmed and r.label_correct and r.confidence >= 0.8
        ),
        "confirmed_incorrect": count(lambda r: r.confirmed and not r.label_correct),
        "unconfirmed_high_confidence": count(
            lambda r: (not r.confirmed) and r.confidence >= 0.8
        ),
        "contradicted_high_confidence": count(
            lambda r: r.contradicted and r.confidence >= 0.8
        ),
        "confirmed_incorrect_cases": [
            {
                "incident": run.benchmark_key,
                "run": run.run,
                "claim_kind": run.claim_kind,
                "expected": run.expected_claim_kind,
                "artifact": run.artifact,
                "confidence": run.confidence,
                "status": run.status,
            }
            for run in runs
            if run.confirmed and not run.label_correct
        ],
    }


def calibration_summary(runs: list[LabeledRun]) -> dict[str, Any]:
    correct = [run.confidence for run in runs if run.label_correct]
    incorrect = [run.confidence for run in runs if not run.label_correct]
    return {
        "mean_confidence_correct": (
            sum(correct) / len(correct) if correct else None
        ),
        "mean_confidence_incorrect": (
            sum(incorrect) / len(incorrect) if incorrect else None
        ),
        "confirmed_but_low_confidence": sum(
            1 for run in runs if run.confirmed and run.confidence < 0.8
        ),
        "confirmed_but_high_confidence": sum(
            1 for run in runs if run.confirmed and run.confidence >= 0.8
        ),
        "p_correct_given_confirmed": (
            sum(1 for run in runs if run.confirmed and run.label_correct)
            / max(sum(1 for run in runs if run.confirmed), 1)
        ),
    }


def analyze(path: Path) -> dict[str, Any]:
    runs = load_labeled_runs([path])
    llm = [run for run in runs if run.claim_source == "llm"]
    fallback = [
        run for run in runs if run.claim_source in {"signal_backed", "heuristic"}
    ]
    incidents = sorted({run.benchmark_key for run in llm})
    return {
        "input": str(path),
        "dataset": {
            "total_runs": len(runs),
            "direct_llm_runs": len(llm),
            "fallback_runs": len(fallback),
            "benchmark_incidents": incidents,
            "distinct_ground_truth_cases": len(incidents),
            "llm_runs_by_incident": {
                key: sum(1 for run in llm if run.benchmark_key == key)
                for key in incidents
            },
        },
        "confidence_distribution_llm": fine_buckets(llm),
        "confirmation_quality_llm": confirmation_quality(llm),
        "threshold_simulation_llm": [
            simulate(llm, name, pred) for name, pred in THRESHOLDS
        ],
        "calibration_llm": calibration_summary(llm),
        "fallback_summary": {
            "count": len(fallback),
            "by_source": {
                source: sum(1 for run in fallback if run.claim_source == source)
                for source in ("signal_backed", "heuristic")
            },
            "excluded_from_llm_metrics": True,
        },
        "safety_llm": {
            "false_resolutions_at_gte_0_8": sum(
                1
                for run in llm
                if run.confirmed and run.confidence >= 0.8 and not run.label_correct
            ),
            "confirmed_incorrect": sum(
                1 for run in llm if run.confirmed and not run.label_correct
            ),
            "confidence_only_resolutions": sum(
                1
                for run in llm
                if (not run.confirmed)
                and run.confidence >= 0.8
                and run.status == "resolved"
            ),
            "contradicted_resolutions": sum(
                1 for run in llm if run.contradicted and run.status == "resolved"
            ),
            "production_gate_resolutions_gte_0_8": sum(
                1 for run in llm if run.confirmed and run.confidence >= 0.8
            ),
        },
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=PROJECT_ROOT / "eval_root_cause_results_r4.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "eval_confidence_calibration_r4.json",
    )
    args = parser.parse_args(argv)
    if not args.input.exists():
        print(f"Missing campaign file: {args.input}")
        return 1
    report = analyze(args.input)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
