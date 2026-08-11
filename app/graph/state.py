"""Shared state schema for the LangGraph investigation workflow.

Deliberately shaped after the Postgres `investigations` row (see
app/db/models.py) for the fields that actually persist there --
investigation_id, issue_description, status, evidence, hypotheses,
final_root_cause. The remaining fields are loop-control and hand-off
scratch (which agents to run this pass, what validation refuted, how
many retries have been spent); those are mirrored into the row's
`workflow_state` JSONB column at every node transition so an
investigation interrupted mid-loop can be resumed at the right pass
with the right agents, rather than restarting from scratch.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional, TypedDict


class EvidenceEntry(TypedDict):
    """One piece of evidence gathered during the investigation. Mirrors
    the shape stored in investigations.evidence (see app/db/models.py).
    `source` names which specialist node produced it: "lineage",
    "sql_analysis", "data_quality", "etl_agent", or "schema_agent"."""

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


# Which kind of bug a hypothesis is claiming, as classified by
# app/graph/validation.py. Determines which direct re-check against the
# sandbox validation_node runs to try to confirm it.
ClaimKind = Literal["join", "stale_pipeline", "schema_change", "duplicates", "unknown"]


class ValidationOutcome(TypedDict):
    """The result of validation_node re-checking the top hypothesis
    against the sandbox warehouse directly (rather than against the
    evidence the specialists already gathered, which would just be
    circular).

    `confirmed` is True only when the direct re-check positively agrees
    with the claim. A claim that the re-check *contradicts* and one it
    simply can't tie to a concrete artifact are both `confirmed=False`;
    `gap` is the short machine-readable reason, used by manager_node to
    pick which agent to send back for more evidence.
    """

    claim_kind: ClaimKind
    confirmed: bool
    checked: str  # what was actually re-read/re-run, for the audit trail
    note: str  # human-readable outcome, fed back into the retry
    gap: str


class InvestigationState(TypedDict):
    """The state threaded through every node of the investigation
    graph. `evidence`, `hypotheses` and `validation_notes` use
    `operator.add` as their reducer so a node can return just the *new*
    items it produced (e.g. `{"evidence": [entry]}`) and LangGraph
    appends them to the running list, rather than every node needing to
    know about and re-return the full history.

    Because the graph is cyclic as of Step 6, those additive lists span
    *all* passes of the loop -- they're an append-only audit trail, not
    a per-pass snapshot. Two consequences the nodes have to respect:
    specialist nodes must not re-add evidence they already added on an
    earlier pass (see _select_new_evidence in app/graph/nodes.py), and
    anything that wants "the current best hypothesis" must read
    `top_hypothesis` rather than taking a max over `hypotheses`, which
    would let a hypothesis an earlier pass already refuted win again.
    """

    investigation_id: Optional[str]
    issue_description: str
    status: str
    evidence: Annotated[list[EvidenceEntry], operator.add]
    hypotheses: Annotated[list[Hypothesis], operator.add]
    final_root_cause: Optional[str]

    # Hand-off between nodes within a single pass. Plain overwrite
    # semantics (no reducer): each is set by exactly one node before
    # being read downstream. A skipped specialist returns nothing at
    # all, so the previous pass's values survive for whichever agents
    # *did* run this pass to keep using.
    agents_to_run: list[str]
    relevant_sql_models: list[RelevantSqlModel]
    relevant_tables: list[str]

    # Loop control (see app/graph/workflow.py). `retry_count` is the
    # number of times validation_node has sent the investigation back to
    # manager_node; `validation_pass_count` is how many times
    # validation_node itself has run (1 initial + up to MAX_RETRIES
    # retries). Both are checked by route_after_validation so a
    # mis-incremented retry_count cannot loop forever.
    # `agents_completed` is every specialist that has run in any pass,
    # so a retry can deliberately pick a *different* one;
    # `follow_up_query` is the refocused retrieval query manager_node
    # builds from what validation couldn't confirm, so a re-run of
    # lineage_agent_node surfaces new context instead of the same hits.
    retry_count: int
    validation_pass_count: int
    agents_completed: list[str]
    follow_up_query: Optional[str]
    top_hypothesis: Optional[Hypothesis]
    validation: Optional[ValidationOutcome]
    validation_notes: Annotated[list[str], operator.add]
