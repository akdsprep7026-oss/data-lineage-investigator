"""R3 confidence-threshold calibration (simulation only).

Reads existing eval JSON captures, re-scores against sandbox ground truth,
and simulates alternate resolve thresholds. Does not change production gates.

Usage:

    python -m tests.eval_confidence_threshold
    python -m tests.eval_confidence_threshold --inputs eval_root_cause_results_r2.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.eval_root_cause import (  # noqa: E402
    RESOLVE_CONFIDENCE_THRESHOLD,
    load_benchmarks,
    root_cause_corresponds,
    score_run,
)

DEFAULT_INPUTS = (
    PROJECT_ROOT / "eval_root_cause_results_r2.json",
    PROJECT_ROOT / "eval_root_cause_results.json",
)

BUCKETS = (
    ("A <=0.60", 0.0, 0.60),
    ("B (0.60,0.70]", 0.60, 0.70),
    ("C (0.70,0.80]", 0.70, 0.80),
    ("D (0.80,0.90]", 0.80, 0.90),
    ("E >0.90", 0.90, 1.01),
)

SIM_THRESHOLDS = (0.70, 0.75, 0.80, 0.85, 0.90)


@dataclass(frozen=True)
class LabeledRun:
    dataset: str
    benchmark_key: str
    run: int
    claim_source: str
    confidence: float
    confirmed: bool
    contradicted: bool
    unknown_claim: bool
    status: str
    claim_kind: Optional[str]
    artifact: Optional[str]
    expected_claim_kind: str
    label_correct: bool
    root_token_ok: bool
    structured_claim_ok: bool
    structured_artifact_ok: bool


def _in_bucket(confidence: float, low: float, high: float, *, first: bool) -> bool:
    if first:
        return confidence <= high
    if high > 1.0:
        return confidence > low
    return low < confidence <= high


def infer_claim_source(capture: dict[str, Any], score: dict[str, Any]) -> str:
    """Use explicit eval instrumentation when present; otherwise unlabeled."""
    explicit = capture.get("claim_source") or score.get("claim_source")
    if explicit in {"llm", "signal_backed", "heuristic"}:
        return explicit
    return "unlabeled"


def is_label_correct(benchmark: Any, capture: dict[str, Any], score: dict[str, Any]) -> bool:
    """Ground-truth claim correctness independent of the resolve gate.

    A claim is correct when structured claim_kind and artifact match the
    benchmark. Root-cause prose token overlap is reported separately and
    is not required for false-resolution simulation (a correct schema
    claim may omit the exact rename tokens in free text).
    """
    del benchmark, capture
    return bool(
        score.get("structured_claim_kind_ok") and score.get("structured_artifact_ok")
    )


def has_root_token_overlap(
    benchmark: Any, capture: dict[str, Any]
) -> bool:
    top = capture.get("top_hypothesis") or {}
    description = top.get("description") or ""
    final_root = capture.get("final_root_cause")
    return root_cause_corresponds(
        benchmark.ground_truth_tokens, final_root or description
    )


def load_labeled_runs(paths: list[Path]) -> list[LabeledRun]:
    benchmarks = {item.key: item for item in load_benchmarks()}
    labeled: list[LabeledRun] = []
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("runs") or []:
            key = str(row.get("benchmark_key"))
            benchmark = benchmarks.get(key)
            if benchmark is None:
                continue
            capture = row.get("capture") or {}
            # Re-score so bucket analysis uses current harness definitions.
            score = score_run(benchmark, capture)
            top = capture.get("top_hypothesis") or {}
            validation = capture.get("validation") or {}
            confirmed = bool(validation.get("confirmed") or score.get("confirmed"))
            gap = (validation.get("gap") or "").lower()
            contradicted = (not confirmed) and (
                "contradict" in (validation.get("note") or "").lower()
                or gap.endswith("_not_present")
                or "wrong" in gap
                or gap in {"join_wrong_model", "join_not_present"}
            )
            claim_kind = top.get("claim_kind") or score.get("structured_claim_kind")
            labeled.append(
                LabeledRun(
                    dataset=path.name,
                    benchmark_key=key,
                    run=int(row.get("run") or 0),
                    claim_source=infer_claim_source(capture, row.get("score") or {}),
                    confidence=float(top.get("confidence_score") or score.get("confidence") or 0.0),
                    confirmed=confirmed,
                    contradicted=contradicted,
                    unknown_claim=claim_kind in (None, "unknown"),
                    status=str(capture.get("status") or score.get("status") or ""),
                    claim_kind=claim_kind,
                    artifact=top.get("artifact"),
                    expected_claim_kind=benchmark.expected_claim_kind,
                    label_correct=is_label_correct(benchmark, capture, score),
                    root_token_ok=has_root_token_overlap(benchmark, capture),
                    structured_claim_ok=bool(score.get("structured_claim_kind_ok")),
                    structured_artifact_ok=bool(score.get("structured_artifact_ok")),
                )
            )
    return labeled


def bucket_metrics(runs: list[LabeledRun]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, (name, low, high) in enumerate(BUCKETS):
        members = [
            run
            for run in runs
            if _in_bucket(run.confidence, low, high, first=index == 0)
        ]
        rows.append(
            {
                "bucket": name,
                "count": len(members),
                "confirmed": sum(1 for run in members if run.confirmed),
                "contradicted": sum(1 for run in members if run.contradicted),
                "unknown": sum(1 for run in members if run.unknown_claim),
                "resolved_current": sum(
                    1
                    for run in members
                    if run.confirmed and run.confidence >= RESOLVE_CONFIDENCE_THRESHOLD
                ),
                "human_review": sum(
                    1 for run in members if run.status == "needs_human_review"
                ),
                "correct": sum(1 for run in members if run.label_correct),
                "false_at_current": sum(
                    1
                    for run in members
                    if run.confirmed
                    and run.confidence >= RESOLVE_CONFIDENCE_THRESHOLD
                    and not run.label_correct
                ),
            }
        )
    return rows


def confirmation_splits(runs: list[LabeledRun]) -> dict[str, Any]:
    def select(pred) -> list[LabeledRun]:
        return [run for run in runs if pred(run)]

    def summarize(members: list[LabeledRun]) -> dict[str, Any]:
        return {
            "count": len(members),
            "correct": sum(1 for run in members if run.label_correct),
            "incorrect": sum(1 for run in members if not run.label_correct),
            "by_source": dict(CounterSource(members)),
        }

    return {
        "confirmed_lt_0_8": summarize(
            select(lambda r: r.confirmed and r.confidence < 0.8)
        ),
        "confirmed_gte_0_8": summarize(
            select(lambda r: r.confirmed and r.confidence >= 0.8)
        ),
        "unconfirmed_gte_0_8": summarize(
            select(lambda r: (not r.confirmed) and r.confidence >= 0.8)
        ),
        "contradicted_gte_0_8": summarize(
            select(lambda r: r.contradicted and r.confidence >= 0.8)
        ),
    }


def CounterSource(runs: list[LabeledRun]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for run in runs:
        counts[run.claim_source] += 1
    return dict(counts)


def simulate_threshold(runs: list[LabeledRun], threshold: float) -> dict[str, Any]:
    n = max(len(runs), 1)

    def clears_gate(confidence: float) -> bool:
        # Production resolve gate is inclusive at the configured threshold.
        if abs(threshold - RESOLVE_CONFIDENCE_THRESHOLD) < 1e-12:
            return confidence >= threshold
        return confidence > threshold

    resolved = [
        run for run in runs if run.confirmed and clears_gate(run.confidence)
    ]
    true_res = [run for run in resolved if run.label_correct]
    false_res = [run for run in resolved if not run.label_correct]
    human = [
        run
        for run in runs
        if not (run.confirmed and clears_gate(run.confidence))
    ]
    confirmed_rejected = [
        run
        for run in runs
        if run.confirmed and not clears_gate(run.confidence)
    ]
    return {
        "threshold": threshold,
        "n": len(runs),
        "resolution_rate": len(resolved) / n,
        "true_resolution_rate": len(true_res) / n,
        "false_resolution_rate": len(false_res) / n,
        "human_review_rate": len(human) / n,
        "confirmed_but_rejected_rate": len(confirmed_rejected) / n,
        "resolved_count": len(resolved),
        "true_resolution_count": len(true_res),
        "false_resolution_count": len(false_res),
        "false_resolution_cases": [
            {
                "dataset": run.dataset,
                "incident": run.benchmark_key,
                "run": run.run,
                "claim_kind": run.claim_kind,
                "expected": run.expected_claim_kind,
                "confidence": run.confidence,
                "source": run.claim_source,
            }
            for run in false_res
        ],
    }


def confirmed_low_confidence_cases(runs: list[LabeledRun]) -> list[dict[str, Any]]:
    cases = []
    for run in runs:
        if not (run.confirmed and run.confidence <= 0.8):
            continue
        cases.append(
            {
                "dataset": run.dataset,
                "incident": run.benchmark_key,
                "run": run.run,
                "claim_source": run.claim_source,
                "claim_kind": run.claim_kind,
                "expected_claim_kind": run.expected_claim_kind,
                "artifact": run.artifact,
                "confidence": run.confidence,
                "confirmed": run.confirmed,
                "status": run.status,
                "label_correct": run.label_correct,
                "root_token_ok": run.root_token_ok,
            }
        )
    return cases


def by_source_summary(runs: list[LabeledRun]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for source in ("llm", "signal_backed", "heuristic", "unlabeled"):
        members = [run for run in runs if run.claim_source == source]
        if not members:
            out[source] = {"count": 0}
            continue
        confidences = [run.confidence for run in members]
        out[source] = {
            "count": len(members),
            "mean_confidence": sum(confidences) / len(confidences),
            "min_confidence": min(confidences),
            "max_confidence": max(confidences),
            "confirmed": sum(1 for run in members if run.confirmed),
            "label_correct": sum(1 for run in members if run.label_correct),
            "resolved_at_0_8": sum(
                1
                for run in members
                if run.confirmed and run.confidence >= 0.8
            ),
        }
    return out


def analyze(paths: list[Path]) -> dict[str, Any]:
    runs = load_labeled_runs(paths)
    llm_runs = [run for run in runs if run.claim_source == "llm"]
    return {
        "inputs": [str(path) for path in paths if path.exists()],
        "dataset": {
            "total_evaluated_runs": len(runs),
            "direct_llm_runs": sum(1 for run in runs if run.claim_source == "llm"),
            "signal_backed_runs": sum(
                1 for run in runs if run.claim_source == "signal_backed"
            ),
            "heuristic_runs": sum(
                1 for run in runs if run.claim_source == "heuristic"
            ),
            "unlabeled_source_runs": sum(
                1 for run in runs if run.claim_source == "unlabeled"
            ),
            "ground_truth_labeled_runs": len(runs),
        },
        "buckets_all": bucket_metrics(runs),
        "buckets_llm_only": bucket_metrics(llm_runs),
        "confirmation_vs_confidence_all": confirmation_splits(runs),
        "confirmation_vs_confidence_llm_only": confirmation_splits(llm_runs),
        "threshold_simulation_all": [
            simulate_threshold(runs, threshold) for threshold in SIM_THRESHOLDS
        ],
        "threshold_simulation_llm_only": [
            simulate_threshold(llm_runs, threshold) for threshold in SIM_THRESHOLDS
        ],
        "confirmed_low_confidence_cases": confirmed_low_confidence_cases(runs),
        "by_source": by_source_summary(runs),
        "current_threshold": RESOLVE_CONFIDENCE_THRESHOLD,
        "safety": {
            "false_resolutions_at_current": sum(
                1
                for run in runs
                if run.confirmed
                and run.confidence >= RESOLVE_CONFIDENCE_THRESHOLD
                and not run.label_correct
            ),
            "confidence_only_resolutions": sum(
                1
                for run in runs
                if (not run.confirmed)
                and run.confidence >= RESOLVE_CONFIDENCE_THRESHOLD
                and run.status == "resolved"
            ),
            "contradicted_resolutions": sum(
                1
                for run in runs
                if run.contradicted and run.status == "resolved"
            ),
            "lowest_threshold_with_zero_false": _lowest_zero_false(runs),
            "lowest_threshold_with_zero_false_llm_only": _lowest_zero_false(llm_runs),
        },
    }


def _lowest_zero_false(runs: list[LabeledRun]) -> Optional[float]:
    if not runs:
        return None
    for threshold in sorted(SIM_THRESHOLDS):
        sim = simulate_threshold(runs, threshold)
        if sim["false_resolution_count"] == 0:
            return threshold
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inputs",
        nargs="*",
        type=Path,
        default=list(DEFAULT_INPUTS),
        help="Eval JSON files to analyze",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "eval_confidence_threshold_r3.json",
    )
    args = parser.parse_args(argv)
    report = analyze(args.inputs)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
