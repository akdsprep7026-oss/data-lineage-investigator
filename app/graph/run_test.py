"""Smoke test for the cyclic (Step 6) investigation graph: applies an
incident scenario (see Step 2 / app/sandbox_data/incidents), runs it
through the graph, and prints the full evidence trail, every hypothesis
proposed, what validation could or couldn't confirm on each pass, how
many loop iterations it took, and the final disposition -- alongside the
incident's ground truth so you can judge whether the investigation
actually landed on the real bug.

Defaults to incident #3 (schema change), the trickier bug called out in
the Step 6 spec.

Usage:
    python -m app.graph.run_test
    python -m app.graph.run_test 1
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.db.investigations import get_investigation
from app.graph.nodes import MAX_RETRIES, RESOLVE_CONFIDENCE_THRESHOLD
from app.graph.workflow import run_investigation
from app.retrieval.ingest import ingest
from app.sandbox_data.incidents import (
    incident_01_join_bug,
    incident_02_stale_pipeline,
    incident_03_schema_change,
    incident_04_duplicate_rows,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

INCIDENTS = {
    "1": (incident_01_join_bug, "app/sandbox_data/incidents/incident_01_join_bug.json"),
    "2": (
        incident_02_stale_pipeline,
        "app/sandbox_data/incidents/incident_02_stale_pipeline.json",
    ),
    "3": (
        incident_03_schema_change,
        "app/sandbox_data/incidents/incident_03_schema_change.json",
    ),
    "4": (
        incident_04_duplicate_rows,
        "app/sandbox_data/incidents/incident_04_duplicate_rows.json",
    ),
}

DEFAULT_INCIDENT = "3"


def _wrap(text: str, width: int = 96, indent: str = "      ") -> str:
    """Hard-wraps long findings so the trail stays readable in a terminal."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return f"\n{indent}".join(lines)


def print_evidence_trail(evidence: list[dict]) -> None:
    print(f"EVIDENCE TRAIL ({len(evidence)} entries, in the order they were gathered)")
    print("-" * 100)
    for index, item in enumerate(evidence, start=1):
        print(
            f"{index:>3}. [{item['source']}] (confidence={item['confidence']:.2f})\n"
            f"      {_wrap(item['finding'])}"
        )
    print()


def main() -> None:
    incident_num = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INCIDENT
    if incident_num not in INCIDENTS:
        print(f"Usage: python -m app.graph.run_test <{'|'.join(INCIDENTS)}>")
        raise SystemExit(1)
    incident_module, incident_json_path = INCIDENTS[incident_num]

    print(f"Applying incident #{incident_num} to the sandbox warehouse...\n")
    incident_module.apply()

    incident = json.loads((PROJECT_ROOT / incident_json_path).read_text(encoding="utf-8"))
    issue_description = incident["issue_description"]
    print(f"\nIssue: {issue_description}\n")

    print("Re-indexing retrieval store against the now-buggy sandbox state...")
    ingest()

    print(
        "Running the cyclic investigation graph (manager -> lineage_agent -> "
        "sql_analysis -> data_quality -> root_cause -> validation -> "
        "{manager | human_review})...\n"
    )
    final_state = run_investigation(issue_description)

    print("=" * 100)
    print_evidence_trail(final_state["evidence"])

    print(f"HYPOTHESES PROPOSED ({len(final_state['hypotheses'])} across all passes)")
    print("-" * 100)
    for index, hypothesis in enumerate(final_state["hypotheses"], start=1):
        print(
            f"{index:>3}. (confidence={hypothesis['confidence_score']:.2f})\n"
            f"      {_wrap(hypothesis['description'])}\n"
            f"      supporting evidence: {hypothesis['supporting_evidence']}"
        )
    print()

    retries = final_state.get("retry_count", 0)
    validation = final_state.get("validation")
    top_hypothesis = final_state.get("top_hypothesis")

    print("LOOP")
    print("-" * 100)
    print(f"  Evidence-gathering passes: {retries + 1} (1 initial + {retries} retries, max {MAX_RETRIES})")
    for index, note in enumerate(final_state.get("validation_notes") or [], start=1):
        print(f"  Retry {index} triggered by:\n      {_wrap(note)}")
    if not final_state.get("validation_notes"):
        print("  No retries: the first hypothesis was confirmed by direct re-check.")
    print()

    print("VALIDATION OF THE FINAL HYPOTHESIS")
    print("-" * 100)
    if validation:
        print(f"  claim kind: {validation['claim_kind']}")
        print(f"  re-checked: {_wrap(validation['checked'])}")
        print(f"  confirmed:  {validation['confirmed']}")
        print(f"  note:       {_wrap(validation['note'])}")
    print()

    print("OUTCOME")
    print("-" * 100)
    print(f"  status: {final_state['status']}")
    if top_hypothesis:
        print(
            f"  top hypothesis (confidence={top_hypothesis['confidence_score']:.2f}, "
            f"auto-resolve threshold > {RESOLVE_CONFIDENCE_THRESHOLD}):\n"
            f"      {_wrap(top_hypothesis['description'])}"
        )
    print(f"  final_root_cause: {_wrap(str(final_state['final_root_cause']))}")
    print()

    print(f"GROUND TRUTH (from {incident_json_path})")
    print("-" * 100)
    print(f"  {_wrap(incident['ground_truth_root_cause'])}")
    print()

    investigation = get_investigation(final_state["investigation_id"])
    print("PERSISTED IN POSTGRES")
    print("-" * 100)
    print(f"  id:               {investigation.id}")
    print(f"  status:           {investigation.status.value}")
    print(
        f"  evidence:         {len(investigation.evidence)} entries "
        f"({', '.join(sorted({item['source'] for item in investigation.evidence}))})"
    )
    print(f"  hypotheses:       {len(investigation.hypotheses)}")
    print(f"  final_root_cause: {'set' if investigation.final_root_cause else 'null'}")
    print(f"  workflow_state:   {json.dumps(investigation.workflow_state, indent=6)[:1200]}")


if __name__ == "__main__":
    main()
