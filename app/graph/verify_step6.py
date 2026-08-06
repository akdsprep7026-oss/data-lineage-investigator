"""Verification harness for the two claims Step 6 has to stand behind:

  1. The retry loop actually triggers -- across the four incident
     scenarios, at least one investigation has its top hypothesis
     refuted by validation_node and goes back to manager_node for
     another, narrower evidence pass.
  2. The Postgres row is updated incrementally, not just at the end.
     This is checked *while the graph is still running*, not by
     inspecting the finished row: each investigation runs on a worker
     thread while the main thread polls `investigations` through a
     separate session and records every observed change.

It also checks the property that makes looping safe to begin with: that
no evidence entry is recorded twice, even though `evidence` uses an
additive reducer across passes.

Runs all four incidents by default (each takes a few minutes against a
real LLM), and resets the sandbox to its clean baseline afterwards.

Pass --offline to clear every configured LLM API key (GOOGLE_API_KEY and
GROQ_API_KEY — see API_KEY_ENV_VARS in app/graph/llm.py) and run against
the deterministic heuristic fallbacks instead. That costs nothing and
doesn't depend on API quota, and it exercises the loop harder than the
real model does: the heuristics can't produce a hypothesis specific
enough for the direct re-check to confirm, so every incident runs the
retry budget down and ends up flagged for human review.

Usage:
    python -m app.graph.verify_step6
    python -m app.graph.verify_step6 --offline
    python -m app.graph.verify_step6 3 4
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from app.db.investigations import create_investigation, get_investigation
from app.graph.llm import API_KEY_ENV_VARS, active_model_label
from app.graph.run_test import INCIDENTS as INCIDENT_FILES
from app.graph.workflow import run_investigation
from app.retrieval.ingest import ingest
from app.sandbox_data.incidents import (
    common,
    incident_01_join_bug,
    incident_02_stale_pipeline,
    incident_03_schema_change,
    incident_04_duplicate_rows,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

INCIDENTS = {
    "1": ("join bug", incident_01_join_bug),
    "2": ("stale pipeline", incident_02_stale_pipeline),
    "3": ("schema change", incident_03_schema_change),
    "4": ("duplicate rows", incident_04_duplicate_rows),
}

# Short enough to catch the fast nodes (the data-quality and validation
# checks run against local SQLite and finish in well under a second).
POLL_INTERVAL_SECONDS = 0.02


def _snapshot(investigation) -> tuple:
    workflow_state = investigation.workflow_state or {}
    return (
        investigation.status.value,
        workflow_state.get("current_node"),
        workflow_state.get("retry_count"),
        len(investigation.evidence),
        len(investigation.hypotheses),
        investigation.final_root_cause is not None,
    )


def run_with_polling(issue_description: str) -> tuple[dict, list[tuple[float, tuple]]]:
    """Runs one investigation on a worker thread while the main thread
    polls its Postgres row, returning the final graph state alongside
    every distinct row state observed *during* the run."""
    investigation = create_investigation(issue_description)
    investigation_id = str(investigation.id)

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def worker() -> None:
        try:
            result["state"] = run_investigation(
                issue_description, investigation_id=investigation_id
            )
        except BaseException as exc:  # surfaced on the main thread below
            error.append(exc)

    thread = threading.Thread(target=worker, name="investigation", daemon=True)
    started = time.monotonic()
    thread.start()

    observations: list[tuple[float, tuple]] = []
    last: Optional[tuple] = None
    while thread.is_alive():
        current = _snapshot(get_investigation(investigation_id))
        if current != last:
            observations.append((time.monotonic() - started, current))
            last = current
        time.sleep(POLL_INTERVAL_SECONDS)
    thread.join()

    if error:
        raise error[0]

    final = _snapshot(get_investigation(investigation_id))
    if final != last:
        observations.append((time.monotonic() - started, final))
    return result["state"], observations


def print_polling_timeline(observations: list[tuple[float, tuple]]) -> None:
    print(
        f"    {'t+s':>7}  {'status':<19} {'node':<14} {'retry':>5} {'evid':>5} "
        f"{'hyp':>4}  root_cause"
    )
    for elapsed, (status, node, retry, evidence, hypotheses, has_root_cause) in observations:
        print(
            f"    {elapsed:>7.1f}  {status:<19} {str(node):<14} {str(retry):>5} "
            f"{evidence:>5} {hypotheses:>4}  {'set' if has_root_cause else '-'}"
        )


def main() -> None:
    arguments = sys.argv[1:]
    offline = "--offline" in arguments
    if offline:
        arguments.remove("--offline")
        for env_var in API_KEY_ENV_VARS.values():
            os.environ.pop(env_var, None)

    selected = arguments or list(INCIDENTS)
    unknown = [key for key in selected if key not in INCIDENTS]
    if unknown:
        print(f"Unknown incident(s): {', '.join(unknown)}. Choose from {', '.join(INCIDENTS)}.")
        raise SystemExit(1)

    print(f"Mode: {active_model_label()}\n")
    summaries: list[dict[str, Any]] = []
    failures: list[str] = []

    for key in selected:
        title, module = INCIDENTS[key]
        print("=" * 100)
        print(f"INCIDENT #{key} ({title})")
        print("=" * 100)
        module.apply()
        ingest()

        _, json_path = INCIDENT_FILES[key]
        incident = json.loads((PROJECT_ROOT / json_path).read_text(encoding="utf-8"))
        issue_description = incident["issue_description"]
        print(f"\nIssue: {issue_description}\n")

        try:
            state, observations = run_with_polling(issue_description)
        except Exception:
            # One incident blowing up (an LLM rate limit, most likely)
            # shouldn't cost us the results for the others.
            print(f"  FAILED: {traceback.format_exc().strip().splitlines()[-1]}\n")
            failures.append(f"#{key} {title}")
            continue

        print("  Postgres row as observed mid-run (one line per observed change):")
        print_polling_timeline(observations)

        evidence = state["evidence"]
        distinct = {(item["source"], item["finding"]) for item in evidence}
        validation = state.get("validation")
        top_hypothesis = state.get("top_hypothesis")

        summaries.append(
            {
                "incident": f"#{key} {title}",
                "retries": state.get("retry_count", 0),
                "passes": state.get("retry_count", 0) + 1,
                "confirmed": bool(validation and validation["confirmed"]),
                "claim_kind": validation["claim_kind"] if validation else "-",
                "status": state["status"],
                "confidence": (
                    top_hypothesis["confidence_score"] if top_hypothesis else 0.0
                ),
                "evidence": len(evidence),
                "duplicate_evidence": len(evidence) - len(distinct),
                "observations": len(observations),
                "sources": sorted({item["source"] for item in evidence}),
            }
        )
        print()

    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    if failures:
        print(f"Incidents that failed to run: {', '.join(failures)}\n")
    if not summaries:
        print("No incident completed, so nothing can be verified.")
        raise SystemExit(1)
    header = (
        f"{'incident':<22} {'passes':>6} {'retries':>7} {'claim':<15} {'confirmed':>9} "
        f"{'conf':>5} {'status':<19} {'evid':>5} {'dupes':>5} {'db-updates':>10}"
    )
    print(header)
    print("-" * len(header))
    for summary in summaries:
        print(
            f"{summary['incident']:<22} {summary['passes']:>6} {summary['retries']:>7} "
            f"{summary['claim_kind']:<15} {str(summary['confirmed']):>9} "
            f"{summary['confidence']:>5.2f} {summary['status']:<19} "
            f"{summary['evidence']:>5} {summary['duplicate_evidence']:>5} "
            f"{summary['observations']:>10}"
        )

    print()
    looped = [s for s in summaries if s["retries"] > 0]
    duplicated = [s for s in summaries if s["duplicate_evidence"] > 0]
    never_incremental = [s for s in summaries if s["observations"] < 3]

    print(
        f"CHECK 1 -- looping triggers: {'PASS' if looped else 'FAIL'} "
        f"({len(looped)} of {len(summaries)} incidents retried: "
        f"{', '.join(s['incident'] for s in looped) or 'none'})"
    )
    print(
        f"CHECK 2 -- incremental Postgres updates: "
        f"{'PASS' if not never_incremental else 'FAIL'} "
        f"(every incident's row was observed changing at least 3 times mid-run; "
        f"min observed = {min(s['observations'] for s in summaries)})"
    )
    print(
        f"CHECK 3 -- no duplicate evidence across passes: "
        f"{'PASS' if not duplicated else 'FAIL'} "
        f"({sum(s['duplicate_evidence'] for s in summaries)} duplicate entries total)"
    )

    print("\nResetting the sandbox warehouse to its clean baseline...")
    common.reset_to_clean_baseline()
    ingest()
    print("Done.")


if __name__ == "__main__":
    main()
