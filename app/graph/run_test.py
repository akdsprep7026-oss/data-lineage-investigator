"""Step 5 smoke test: applies an incident scenario (see Step 2 /
app/sandbox_data/incidents), creates a new investigation for it, runs
it through the linear investigation graph (manager -> lineage_agent ->
sql_analysis -> data_quality -> root_cause), and prints the ranked
hypotheses that come out -- alongside the incident's ground truth, so
you can eyeball whether root_cause_node's top hypothesis actually
points at the real bug.

Defaults to incident #1 (join bug), which is the specific check called
out in the Step 5 spec: "run it against incident #1 (join bug). Does
root_cause_node's top hypothesis actually point at the join issue?"

Usage:
    python -m app.graph.run_test
    python -m app.graph.run_test 2
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.db.investigations import get_investigation
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


def main() -> None:
    incident_num = sys.argv[1] if len(sys.argv) > 1 else "1"
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

    print("Running the investigation graph (manager -> lineage_agent -> "
          "sql_analysis -> data_quality -> root_cause)...\n")
    final_state = run_investigation(issue_description)

    ranked = sorted(
        final_state["hypotheses"], key=lambda h: h["confidence_score"], reverse=True
    )
    print(f"{len(ranked)} hypothesis(es):\n")
    for rank, hypothesis in enumerate(ranked, start=1):
        print(
            f"{rank}. (confidence={hypothesis['confidence_score']:.2f}) "
            f"{hypothesis['description']}"
        )
        print(f"   supporting evidence: {hypothesis['supporting_evidence']}")

    print(f"\nGround truth root cause (from {incident_json_path}):")
    print(f"  {incident['ground_truth_root_cause']}")

    investigation = get_investigation(final_state["investigation_id"])
    print(
        f"\nPersisted investigation {investigation.id}: "
        f"status={investigation.status.value}, "
        f"{len(investigation.evidence)} evidence entries "
        f"({', '.join(sorted({e['source'] for e in investigation.evidence}))}), "
        f"{len(investigation.hypotheses)} hypotheses."
    )


if __name__ == "__main__":
    main()
