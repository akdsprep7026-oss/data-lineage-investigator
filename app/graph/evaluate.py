"""Step 7 evaluation harness: runs every incident scenario through the
full graph end to end and reports the predicted root cause against the
incident's ground truth, so a human (or a future automated judge) can
score the match rate.

This is deliberately a thin wrapper around app.graph.run_test's per-
incident plumbing (see INCIDENTS there) -- it just loops over every
incident instead of one, and prints a comparison table instead of the
full evidence trail.

Usage:
    python -m app.graph.evaluate            # all 4 incidents
    python -m app.graph.evaluate 1 3         # just these

Set LLM_PROVIDER=groq in the environment to run this against Groq
instead of the default (Gemini) -- useful for dev/debug iterations that
shouldn't eat into Gemini's small daily quota.
"""

from __future__ import annotations

import json
import sys
import textwrap
import traceback
from pathlib import Path
from typing import Any, Optional

from app.graph.llm import active_model_label
from app.graph.run_test import INCIDENTS
from app.graph.workflow import run_investigation
from app.retrieval.ingest import ingest
from app.sandbox_data.incidents import common

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_one(key: str) -> dict[str, Any]:
    module, json_path = INCIDENTS[key]
    module.apply()
    ingest()

    incident = json.loads((PROJECT_ROOT / json_path).read_text(encoding="utf-8"))
    issue_description = incident["issue_description"]
    final_state = run_investigation(issue_description)

    top_hypothesis = final_state.get("top_hypothesis")
    predicted = final_state.get("final_root_cause") or (
        top_hypothesis["description"] if top_hypothesis else None
    )
    sources = {item["source"] for item in final_state["evidence"]} - {"validation"}

    return {
        "incident": key,
        "title": incident["title"],
        "issue": issue_description,
        "predicted": predicted,
        "ground_truth": incident["ground_truth_root_cause"],
        "confidence": top_hypothesis["confidence_score"] if top_hypothesis else 0.0,
        "status": final_state["status"],
        "agents_run": sorted(sources),
        "retries": final_state.get("retry_count", 0),
    }


def _wrap(text: Optional[str], width: int = 92, indent: str = "               ") -> str:
    if not text:
        return "(none)"
    return textwrap.fill(
        text, width=width, initial_indent="", subsequent_indent=indent
    )


def main() -> None:
    selected = sys.argv[1:] or list(INCIDENTS)
    unknown = [key for key in selected if key not in INCIDENTS]
    if unknown:
        print(f"Unknown incident(s): {', '.join(unknown)}. Choose from {', '.join(INCIDENTS)}.")
        raise SystemExit(1)

    print(f"LLM: {active_model_label()}\n")
    results: list[dict[str, Any]] = []
    failures: list[str] = []

    for key in selected:
        print(f"Running incident #{key}...")
        try:
            result = run_one(key)
        except Exception:
            print(f"  FAILED: {traceback.format_exc().strip().splitlines()[-1]}")
            failures.append(key)
            continue
        results.append(result)
        print(
            f"  status={result['status']} confidence={result['confidence']:.2f} "
            f"retries={result['retries']} agents={result['agents_run']}"
        )

    print()
    print("=" * 100)
    print("DETAIL")
    print("=" * 100)
    for r in results:
        print(f"\nINCIDENT #{r['incident']} -- {r['title']}")
        print(f"  Issue:         {_wrap(r['issue'])}")
        print(f"  Agents run:    {r['agents_run']}")
        print(f"  Predicted:     {_wrap(r['predicted'])}")
        print(f"  Ground truth:  {_wrap(r['ground_truth'])}")
        print(f"  Confidence:    {r['confidence']:.2f}   Status: {r['status']}   Retries: {r['retries']}")

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    if failures:
        print(f"Incidents that failed to run: {', '.join(failures)}\n")
    header = f"{'incident':<10} {'confidence':>10}   {'status':<19} {'agents_run'}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(f"#{r['incident']:<9} {r['confidence']:>10.2f}   {r['status']:<19} {r['agents_run']}")

    out_path = PROJECT_ROOT / "app" / "graph" / "eval_results.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nSaved raw results (predicted vs. ground truth text) to {out_path}")

    print("\nResetting the sandbox warehouse to its clean baseline...")
    common.reset_to_clean_baseline()
    ingest()
    print("Done.")


if __name__ == "__main__":
    main()
