"""Shared state schema for the LangGraph investigation workflow.

Deliberately shaped after the Postgres `investigations` row (see
app/db/models.py) for the fields that actually persist there --
investigation_id, issue_description, status, evidence, hypotheses,
final_root_cause. A few extra scratch fields (agents_to_run,
relevant_sql_models, relevant_tables) are threaded between nodes purely
to hand off intermediate findings (e.g. lineage_agent_node telling
sql_analysis_node and data_quality_node what to look at); those are
*not* written to Postgres -- only evidence/hypotheses/status/
final_root_cause are, via app/db/investigations.py.
"""

from __future__ import annotations

import operator
from typing import Annotated, Optional, TypedDict


class EvidenceEntry(TypedDict):
    """One piece of evidence gathered during the investigation. Mirrors
    the shape stored in investigations.evidence (see app/db/models.py).
    `source` names which specialist node produced it: "lineage",
    "sql_analysis", or "data_quality"."""

    source: str
    finding: str
    confidence: float


class Hypothesis(TypedDict):
    """One candidate root-cause explanation, ranked by confidence.
    Mirrors the shape stored in investigations.hypotheses (see
    app/db/models.py)."""

    description: str
    supporting_evidence: list[str]
    confidence_score: float


class RelevantSqlModel(TypedDict):
    """A SQL model file lineage_agent_node flagged as relevant, handed
    off to sql_analysis_node to review."""

    file_path: str
    table_name: str
    sql_text: str


class InvestigationState(TypedDict):
    """The state threaded through every node of the investigation
    graph. `evidence` and `hypotheses` use `operator.add` as their
    reducer so a node can return just the *new* items it produced (e.g.
    `{"evidence": [entry]}`) and LangGraph appends them to the running
    list, rather than every node needing to know about and re-return
    the full history.
    """

    investigation_id: Optional[str]
    issue_description: str
    status: str
    evidence: Annotated[list[EvidenceEntry], operator.add]
    hypotheses: Annotated[list[Hypothesis], operator.add]
    final_root_cause: Optional[str]

    # Scratch fields for hand-off between nodes only -- see module
    # docstring. Plain overwrite semantics (no reducer): each is set
    # once, by exactly one node, before being read downstream.
    agents_to_run: list[str]
    relevant_sql_models: list[RelevantSqlModel]
    relevant_tables: list[str]
