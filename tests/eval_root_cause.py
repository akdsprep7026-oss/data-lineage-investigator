"""Root-cause accuracy evaluation harness (measurement only).

Extends the existing sandbox incident definitions used by
``tests/run_eval.py`` / ``app.graph.run_test.INCIDENTS`` with structured
claim-kind / artifact scoring against the live investigation pipeline.

Usage (from project root):

    $env:LLM_PROVIDER=\"groq\"; python -m tests.eval_root_cause
    $env:LLM_PROVIDER=\"groq\"; python -m tests.eval_root_cause --runs 3

Does not modify production resolution or validation gates.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from app.graph import root_cause as root_cause_module  # noqa: E402
from app.graph.run_test import INCIDENTS  # noqa: E402
from app.graph.validation import resolve_claim_kind  # noqa: E402
from app.graph.workflow import run_investigation  # noqa: E402
from app.retrieval.ingest import ingest  # noqa: E402
from app.sandbox_data.incidents import common  # noqa: E402

RESOLVE_CONFIDENCE_THRESHOLD = 0.8
CHECKABLE = frozenset({"join", "stale_pipeline", "schema_change", "duplicates"})
CLAIM_SOURCE_LLM = "llm"
CLAIM_SOURCE_SIGNAL = "signal_backed"
CLAIM_SOURCE_HEURISTIC = "heuristic"


@dataclass(frozen=True)
class BenchmarkIncident:
    """Authoritative expectations derived from sandbox incident JSON + kind."""

    key: str
    title: str
    issue_description: str
    ground_truth_root_cause: str
    expected_claim_kind: str
    expected_artifacts: tuple[str, ...]
    ground_truth_tokens: tuple[str, ...]
    expected_validation_confirmed: bool = True
    expected_status_if_correct: str = "resolved"


def _normalize_artifact(value: Optional[str]) -> str:
    text = (value or "").strip().lower().replace("\\", "/")
    if "sql_models/" in text:
        text = text[text.index("sql_models/") :]
    return text


def load_benchmarks() -> list[BenchmarkIncident]:
    """Build benchmarks from repository incident JSON + expected kinds.

    Expected claim kinds map 1:1 to the four sandbox scenarios and the
    four checkable ClaimKinds in ``app.graph.validation``. Acceptable
    artifacts come from each incident's ``ground_truth_location``.
    """
    specs: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
        "1": (
            "join",
            (
                "sql_models/01_stg_orders_cleaned.sql",
                "01_stg_orders_cleaned.sql",
                "stg_orders_cleaned.sql",
                "stg_orders_cleaned",
                "build_stg_orders_cleaned",
            ),
            ("inner join", "stg_orders_cleaned", "raw_customers"),
        ),
        "2": (
            "stale_pipeline",
            (
                "build_fct_daily_revenue",
                "fct_daily_revenue",
            ),
            ("build_fct_daily_revenue", "fail", "stale"),
        ),
        "3": (
            "schema_change",
            (
                "sql_models/01_stg_orders_cleaned.sql",
                "01_stg_orders_cleaned.sql",
                "stg_orders_cleaned.sql",
                "stg_orders_cleaned",
                "build_stg_orders_cleaned",
                "raw_orders",
                "created_at",
                "order_created_at",
            ),
            ("created_at", "order_created_at", "stg_orders_cleaned"),
        ),
        "4": (
            "duplicates",
            (
                "sql_models/01_stg_orders_cleaned.sql",
                "01_stg_orders_cleaned.sql",
                "stg_orders_cleaned.sql",
                "stg_orders_cleaned",
                "build_stg_orders_cleaned",
                "raw_orders",
                "order_id",
            ),
            ("duplicate", "order_id", "raw_orders"),
        ),
    }
    benchmarks: list[BenchmarkIncident] = []
    for key, (kind, artifacts, tokens) in specs.items():
        _module, json_path = INCIDENTS[key]
        incident = json.loads((PROJECT_ROOT / json_path).read_text(encoding="utf-8"))
        benchmarks.append(
            BenchmarkIncident(
                key=key,
                title=incident["title"],
                issue_description=incident["issue_description"],
                ground_truth_root_cause=incident["ground_truth_root_cause"],
                expected_claim_kind=kind,
                expected_artifacts=artifacts,
                ground_truth_tokens=tokens,
            )
        )
    return benchmarks


def artifact_matches(expected: tuple[str, ...], artifact: Optional[str], description: str) -> bool:
    haystack = f"{_normalize_artifact(artifact)} {description.lower()}"
    return any(_normalize_artifact(item) and _normalize_artifact(item) in haystack for item in expected)


def root_cause_corresponds(tokens: tuple[str, ...], text: Optional[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    hits = sum(1 for token in tokens if token.lower() in lowered)
    # Require a majority of distinctive tokens so a vague paraphrase fails.
    return hits >= max(1, (len(tokens) + 1) // 2)


def score_run(benchmark: BenchmarkIncident, capture: dict[str, Any]) -> dict[str, Any]:
    """Score one investigation capture against benchmark ground truth."""
    top = capture.get("top_hypothesis") or {}
    validation = capture.get("validation") or {}
    description = top.get("description") or ""
    claim_kind = top.get("claim_kind")
    resolved_kind = capture.get("resolve_claim_kind") or (
        resolve_claim_kind(top) if top else None
    )
    confidence = float(top.get("confidence_score") or 0.0)
    confirmed = bool(validation.get("confirmed"))
    status = capture.get("status") or ""
    final_root = capture.get("final_root_cause")
    artifact = top.get("artifact")

    structured_claim_ok = claim_kind == benchmark.expected_claim_kind
    fallback_claim_ok = resolved_kind == benchmark.expected_claim_kind
    # Keep claim_kind_ok aligned with structured accuracy (primary R1 goal).
    claim_kind_ok = structured_claim_ok

    structured_artifact_ok = artifact_matches(
        benchmark.expected_artifacts, artifact, ""
    )
    fallback_artifact_ok = artifact_matches(
        benchmark.expected_artifacts, artifact, description
    )
    artifact_ok = structured_artifact_ok

    confirmation_ok = confirmed is True
    resolved = status == "resolved"
    human_review = status == "needs_human_review"
    root_ok = root_cause_corresponds(
        benchmark.ground_truth_tokens, final_root or description
    )

    end_to_end = (
        structured_claim_ok
        and structured_artifact_ok
        and confirmation_ok
        and confidence >= RESOLVE_CONFIDENCE_THRESHOLD
        and resolved
        and root_ok
    )

    # False resolution = resolved with the wrong claim (safety). Artifact
    # mismatches are tracked separately via structured_artifact_ok / e2e.
    false_resolution = resolved and not structured_claim_ok
    unknown_resolved = resolved and (
        resolved_kind == "unknown" or claim_kind == "unknown"
    )
    contradicted_resolved = resolved and confirmed is False
    confidence_only_resolved = (
        resolved and confirmed is False and confidence >= RESOLVE_CONFIDENCE_THRESHOLD
    )

    # Prefer resolved_kind for failure taxonomy (what validation actually ran).
    failure_mode = None
    if capture.get("crashed"):
        failure_mode = "runtime_failure"
    elif end_to_end:
        failure_mode = None
    elif not structured_claim_ok and (claim_kind == "unknown" or not claim_kind):
        failure_mode = "unknown_unclassifiable"
    elif not structured_claim_ok:
        failure_mode = "wrong_claim_kind"
    elif not structured_artifact_ok:
        failure_mode = "wrong_artifact"
    elif structured_claim_ok and not confirmed:
        failure_mode = "correct_kind_but_validator_contradiction"
    elif structured_claim_ok and confirmed and confidence < RESOLVE_CONFIDENCE_THRESHOLD:
        failure_mode = "correct_hypothesis_low_confidence"
    elif structured_claim_ok and confirmed and not resolved:
        failure_mode = "confirmed_but_not_resolved"
    else:
        failure_mode = "other"

    return {
        "claim_kind_ok": claim_kind_ok,
        "structured_claim_kind_ok": structured_claim_ok,
        "fallback_claim_kind_ok": fallback_claim_ok,
        "artifact_ok": artifact_ok,
        "structured_artifact_ok": structured_artifact_ok,
        "fallback_artifact_ok": fallback_artifact_ok,
        "confirmation_ok": confirmation_ok,
        "resolved": resolved,
        "human_review": human_review,
        "end_to_end_correct": end_to_end,
        "false_resolution": false_resolution,
        "unknown_resolved": unknown_resolved,
        "contradicted_resolved": contradicted_resolved,
        "confidence_only_resolved": confidence_only_resolved,
        "failure_mode": failure_mode,
        "resolved_kind": resolved_kind,
        "structured_claim_kind": claim_kind,
        "confidence": confidence,
        "confirmed": confirmed,
        "status": status,
    }


@dataclass
class AggregateMetrics:
    n: int = 0
    claim_kind_hits: int = 0
    structured_claim_hits: int = 0
    fallback_claim_hits: int = 0
    artifact_hits: int = 0
    structured_artifact_hits: int = 0
    fallback_artifact_hits: int = 0
    confirmation_hits: int = 0
    resolution_hits: int = 0
    human_review_hits: int = 0
    false_resolutions: int = 0
    end_to_end_hits: int = 0
    unknown_resolved: int = 0
    contradicted_resolved: int = 0
    confidence_only_resolved: int = 0
    failure_modes: Counter = field(default_factory=Counter)

    def add(self, score: dict[str, Any]) -> None:
        self.n += 1
        self.claim_kind_hits += int(score["claim_kind_ok"])
        self.structured_claim_hits += int(score.get("structured_claim_kind_ok", False))
        self.fallback_claim_hits += int(score.get("fallback_claim_kind_ok", False))
        self.artifact_hits += int(score["artifact_ok"])
        self.structured_artifact_hits += int(score.get("structured_artifact_ok", False))
        self.fallback_artifact_hits += int(score.get("fallback_artifact_ok", False))
        self.confirmation_hits += int(score["confirmation_ok"])
        self.resolution_hits += int(score["resolved"])
        self.human_review_hits += int(score["human_review"])
        self.false_resolutions += int(score["false_resolution"])
        self.end_to_end_hits += int(score["end_to_end_correct"])
        self.unknown_resolved += int(score["unknown_resolved"])
        self.contradicted_resolved += int(score["contradicted_resolved"])
        self.confidence_only_resolved += int(score["confidence_only_resolved"])
        mode = score.get("failure_mode")
        if mode:
            self.failure_modes[mode] += 1

    def rates(self) -> dict[str, Any]:
        n = max(self.n, 1)
        return {
            "n": self.n,
            "claim_kind_accuracy": self.claim_kind_hits / n,
            "structured_claim_kind_accuracy": self.structured_claim_hits / n,
            "fallback_claim_kind_accuracy": self.fallback_claim_hits / n,
            "artifact_accuracy": self.artifact_hits / n,
            "structured_artifact_accuracy": self.structured_artifact_hits / n,
            "fallback_artifact_accuracy": self.fallback_artifact_hits / n,
            "validation_confirmation_rate": self.confirmation_hits / n,
            "resolution_rate": self.resolution_hits / n,
            "human_review_rate": self.human_review_hits / n,
            "false_resolution_rate": self.false_resolutions / n,
            "end_to_end_correctness": self.end_to_end_hits / n,
            "unknown_resolved": self.unknown_resolved,
            "contradicted_resolved": self.contradicted_resolved,
            "confidence_only_resolved": self.confidence_only_resolved,
            "failure_modes": dict(self.failure_modes),
        }


def capture_from_final_state(final_state: dict[str, Any]) -> dict[str, Any]:
    top = final_state.get("top_hypothesis") or {}
    validation = final_state.get("validation") or {}
    return {
        "crashed": False,
        "status": final_state.get("status"),
        "investigation_id": final_state.get("investigation_id"),
        "retry_count": final_state.get("retry_count", 0),
        "validation_pass_count": final_state.get("validation_pass_count", 0),
        "top_hypothesis": {
            "description": top.get("description"),
            "confidence_score": top.get("confidence_score"),
            "claim_kind": top.get("claim_kind"),
            "artifact": top.get("artifact"),
            "failure_mode": top.get("failure_mode"),
        },
        "resolve_claim_kind": resolve_claim_kind(top) if top else None,
        "validation": {
            "confirmed": validation.get("confirmed"),
            "claim_kind": validation.get("claim_kind"),
            "gap": validation.get("gap"),
            "note": validation.get("note"),
        },
        "final_root_cause": final_state.get("final_root_cause"),
        "evidence_count": len(final_state.get("evidence") or []),
        "hypothesis_count": len(final_state.get("hypotheses") or []),
    }


@contextmanager
def track_claim_source() -> Iterator[dict[str, Any]]:
    """Eval-only instrumentation: record whether each root-cause synthesis
    came from the LLM, signal-backed fallback, or offline heuristic.

    Does not alter production branching; only observes function entry.
    """
    state: dict[str, Any] = {"events": [], "final": None}
    original_invoke = root_cause_module.invoke_structured
    original_signal = root_cause_module.signal_backed_hypotheses
    original_heuristic = root_cause_module._heuristic_generate_hypotheses

    def watched_invoke(*args: Any, **kwargs: Any) -> Any:
        purpose = kwargs.get("purpose")
        if purpose is None and len(args) >= 3:
            purpose = args[2]
        result = original_invoke(*args, **kwargs)
        if purpose == "root-cause synthesis":
            state["events"].append(CLAIM_SOURCE_LLM)
            state["final"] = CLAIM_SOURCE_LLM
        return result

    def watched_signal(*args: Any, **kwargs: Any) -> Any:
        result = original_signal(*args, **kwargs)
        if result:
            state["events"].append(CLAIM_SOURCE_SIGNAL)
            state["final"] = CLAIM_SOURCE_SIGNAL
        return result

    def watched_heuristic(*args: Any, **kwargs: Any) -> Any:
        state["events"].append(CLAIM_SOURCE_HEURISTIC)
        state["final"] = CLAIM_SOURCE_HEURISTIC
        return original_heuristic(*args, **kwargs)

    root_cause_module.invoke_structured = watched_invoke  # type: ignore[assignment]
    root_cause_module.signal_backed_hypotheses = watched_signal  # type: ignore[assignment]
    root_cause_module._heuristic_generate_hypotheses = watched_heuristic  # type: ignore[assignment]
    try:
        yield state
    finally:
        root_cause_module.invoke_structured = original_invoke  # type: ignore[assignment]
        root_cause_module.signal_backed_hypotheses = original_signal  # type: ignore[assignment]
        root_cause_module._heuristic_generate_hypotheses = original_heuristic  # type: ignore[assignment]


def run_one_benchmark(benchmark: BenchmarkIncident) -> dict[str, Any]:
    module, _json_path = INCIDENTS[benchmark.key]
    module.apply()
    ingest()
    claim_source = None
    claim_source_events: list[str] = []
    try:
        with track_claim_source() as source_state:
            final_state = run_investigation(benchmark.issue_description)
            claim_source = source_state.get("final")
            claim_source_events = list(source_state.get("events") or [])
        capture = capture_from_final_state(final_state)
        capture["claim_source"] = claim_source
        capture["claim_source_events"] = claim_source_events
    except Exception as exc:  # noqa: BLE001 - evaluation must record failures
        capture = {
            "crashed": True,
            "error_type": type(exc).__name__,
            "error": str(exc)[:400],
            "status": "crashed",
            "top_hypothesis": {},
            "validation": {},
            "final_root_cause": None,
            "resolve_claim_kind": None,
            "claim_source": claim_source,
            "claim_source_events": claim_source_events,
        }
    finally:
        try:
            common.reset_to_clean_baseline()
        except Exception:  # noqa: BLE001
            pass
    score = score_run(benchmark, capture)
    score["claim_source"] = capture.get("claim_source")
    return {"benchmark_key": benchmark.key, "capture": capture, "score": score}


def consistency(values: list[Any]) -> tuple[int, int]:
    if not values:
        return 0, 0
    mode = Counter(values).most_common(1)[0][0]
    return sum(1 for item in values if item == mode), len(values)


def _subset_rates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    agg = AggregateMetrics()
    unknown_claims = 0
    for row in rows:
        agg.add(row["score"])
        kind = (row["capture"].get("top_hypothesis") or {}).get("claim_kind")
        if kind == "unknown" or not kind:
            unknown_claims += 1
    rates = agg.rates()
    rates["unknown_claims"] = unknown_claims
    return rates


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="Runs per incident")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "eval_root_cause_results.json",
        help="Where to write JSON results",
    )
    args = parser.parse_args(argv)

    benchmarks = load_benchmarks()
    all_results: list[dict[str, Any]] = []
    aggregate = AggregateMetrics()

    for benchmark in benchmarks:
        for run_idx in range(1, args.runs + 1):
            print(f"Running incident #{benchmark.key} run {run_idx}/{args.runs}...")
            result = run_one_benchmark(benchmark)
            result["run"] = run_idx
            all_results.append(result)
            aggregate.add(result["score"])
            score = result["score"]
            source = score.get("claim_source")
            print(
                f"  source={source} status={score['status']} "
                f"kind={score.get('structured_claim_kind')} "
                f"artifact={(result['capture'].get('top_hypothesis') or {}).get('artifact')} "
                f"confirmed={score['confirmed']} conf={score['confidence']:.2f} "
                f"e2e={score['end_to_end_correct']} fail={score['failure_mode']}"
            )

    rates = aggregate.rates()
    llm_rows = [
        row
        for row in all_results
        if row["score"].get("claim_source") == CLAIM_SOURCE_LLM
    ]
    signal_rows = [
        row
        for row in all_results
        if row["score"].get("claim_source") == CLAIM_SOURCE_SIGNAL
    ]
    heuristic_rows = [
        row
        for row in all_results
        if row["score"].get("claim_source") == CLAIM_SOURCE_HEURISTIC
    ]
    llm_only = _subset_rates(llm_rows) if llm_rows else {"n": 0}
    by_source = {
        "llm": len(llm_rows),
        "signal_backed": len(signal_rows),
        "heuristic": len(heuristic_rows),
        "unknown_source": sum(
            1 for row in all_results if not row["score"].get("claim_source")
        ),
    }

    by_incident: dict[str, Any] = {}
    for benchmark in benchmarks:
        rows = [row for row in all_results if row["benchmark_key"] == benchmark.key]
        kinds = [
            (row["capture"].get("top_hypothesis") or {}).get("claim_kind")
            for row in rows
        ]
        resolved_flags = [row["score"]["resolved"] for row in rows]
        confirmed_flags = [row["score"]["confirmed"] for row in rows]
        kind_hits, kind_n = consistency(kinds)
        res_hits, res_n = consistency(resolved_flags)
        conf_hits, conf_n = consistency(confirmed_flags)
        by_incident[benchmark.key] = {
            "title": benchmark.title,
            "expected_claim_kind": benchmark.expected_claim_kind,
            "runs": [
                {
                    "run": row["run"],
                    "claim_source": row["score"].get("claim_source"),
                    "status": row["score"]["status"],
                    "structured_claim_kind": row["score"].get("structured_claim_kind"),
                    "resolved_kind": row["score"]["resolved_kind"],
                    "confirmed": row["score"]["confirmed"],
                    "confidence": row["score"]["confidence"],
                    "end_to_end_correct": row["score"]["end_to_end_correct"],
                    "failure_mode": row["score"]["failure_mode"],
                    "artifact": (row["capture"].get("top_hypothesis") or {}).get(
                        "artifact"
                    ),
                    "hyp_failure_mode": (row["capture"].get("top_hypothesis") or {}).get(
                        "failure_mode"
                    ),
                }
                for row in rows
            ],
            "claim_kind_consistency": f"{kind_hits}/{kind_n}",
            "structured_claim_kind_consistency": f"{kind_hits}/{kind_n}",
            "resolution_consistency": f"{res_hits}/{res_n}",
            "confirmation_consistency": f"{conf_hits}/{conf_n}",
        }

    unsafe = (
        rates["false_resolution_rate"] > 0
        or rates["unknown_resolved"] > 0
        or rates["contradicted_resolved"] > 0
        or rates["confidence_only_resolved"] > 0
    )
    payload = {
        "benchmarks": [asdict(item) for item in benchmarks],
        "runs": all_results,
        "aggregate": rates,
        "by_source": by_source,
        "llm_only": llm_only,
        "by_incident": by_incident,
        "unsafe": unsafe,
    }
    args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {args.output}")
    print("by_source:", json.dumps(by_source, indent=2))
    print("llm_only:", json.dumps(llm_only, indent=2))
    print("aggregate:", json.dumps(rates, indent=2))
    if unsafe:
        print("\nSTOP: unsafe resolution behavior detected.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
