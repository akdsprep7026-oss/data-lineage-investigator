"""Node functions for the investigation LangGraph (see
app/graph/workflow.py for how these are wired -- as of Step 6 the graph
is cyclic: validation_node can route back to manager_node for up to
MAX_RETRIES further passes before the investigation is handed to
human_review_node).

Each node returns a partial InvestigationState update for LangGraph to
merge (see app/graph/state.py for the reducers governing how evidence/
hypotheses accumulate) and writes to the `investigations` table in
Postgres via app/db/investigations.py *on entry and on exit*, so an
investigation interrupted at any point can be resumed by id with both
its findings and its position in the loop intact.

Two consequences of the graph being cyclic that every node here has to
respect:

  - `evidence` and `hypotheses` are append-only across all passes, so a
    specialist re-running on a retry must contribute only what's new.
    Both defences the spec allows are in place: manager_node schedules
    only the agents that can close the specific gap validation found
    (never all three again), and _select_new_evidence drops anything
    matching a (source, finding) already recorded, so even an agent that
    re-derives an identical result adds nothing the second time.
  - "the current best hypothesis" means `top_hypothesis` (whatever the
    latest pass produced), never a max over the accumulated
    `hypotheses` list -- otherwise a high-confidence hypothesis that an
    earlier pass already refuted would keep winning.
"""

from __future__ import annotations

from typing import Any, Optional

from app.db.investigations import (
    create_investigation,
    get_investigation,
    update_investigation,
)
from app.db.models import InvestigationStatus
from app.graph.data_quality import run_basic_checks
from app.graph.root_cause import get_root_cause_generator
from app.graph.sql_review import get_sql_reviewer
from app.graph.state import (
    EvidenceEntry,
    Hypothesis,
    InvestigationState,
    RelevantSqlModel,
    ValidationOutcome,
)
from app.graph.validation import validate_hypothesis
from app.retrieval.retriever import retrieve

# Every specialist agent the manager can dispatch. The first pass runs
# all of them; a retry pass deliberately runs a targeted subset (see
# RETRY_PLAN).
AVAILABLE_AGENTS = ["lineage_agent", "sql_analysis", "data_quality"]

# How many times validation_node may send an investigation back to
# manager_node before it has to be handed off for review regardless.
MAX_RETRIES = 2

# Above this, human_review_node closes the investigation as resolved;
# at or below it, the investigation is flagged for a human instead of
# asserting a conclusion the evidence doesn't support.
RESOLVE_CONFIDENCE_THRESHOLD = 0.8

# Even a hypothesis the re-check agrees with is worth another evidence
# pass if the confidence behind it is this weak.
WELL_SUPPORTED_CONFIDENCE = 0.7


class RetryPlan(dict):
    """A targeted retry: which specialist to bring in to close the gap
    validation_node found, and what to refocus retrieval on."""


# Maps the `gap` reported by app/graph/validation.py to the specialist
# best placed to close it. The rule of thumb is to pivot to a *different
# kind* of evidence than the one that just failed to hold up: if a claim
# about SQL was contradicted by re-reading the SQL, go look at the data;
# if a claim about job metadata was contradicted by re-reading that
# metadata, go look at the transformation logic.
RETRY_PLAN: dict[str, RetryPlan] = {
    "join_not_present": RetryPlan(
        agent="data_quality",
        focus="row counts, null counts and duplicate keys in the underlying tables",
    ),
    "join_model_unnamed": RetryPlan(
        agent="sql_analysis",
        focus="which specific SQL model performs the join that drops rows",
    ),
    "join_wrong_model": RetryPlan(
        agent="sql_analysis",
        focus="which specific SQL model performs the join that drops rows",
    ),
    "pipeline_healthy": RetryPlan(
        agent="data_quality",
        focus="actual row counts and coverage of the affected tables",
    ),
    "pipeline_job_unnamed": RetryPlan(
        agent="data_quality",
        focus="which table is actually missing rows and for which dates",
    ),
    "pipeline_wrong_job": RetryPlan(
        agent="data_quality",
        focus="which table is actually missing rows and for which dates",
    ),
    "schema_intact": RetryPlan(
        agent="sql_analysis",
        focus="join, filter and de-duplication logic in the transformation models",
    ),
    "schema_model_unnamed": RetryPlan(
        agent="sql_analysis",
        focus="which SQL model references the renamed or missing column",
    ),
    "duplicates_absent": RetryPlan(
        agent="sql_analysis",
        focus="filters and aggregation logic that could distort the reported totals",
    ),
    "table_missing": RetryPlan(
        agent="data_quality",
        focus="which tables actually exist in the warehouse and their row counts",
    ),
    "unclassifiable_claim": RetryPlan(
        agent="data_quality",
        focus="concrete row counts, nulls and duplicates in the affected tables",
    ),
    "no_hypothesis": RetryPlan(
        agent="data_quality",
        focus="concrete row counts, nulls and duplicates in the affected tables",
    ),
}

DEFAULT_RETRY_PLAN = RetryPlan(
    agent="data_quality",
    focus="concrete row counts, nulls and duplicates in the affected tables",
)


def _workflow_snapshot(state: InvestigationState, node: str, **extra: Any) -> dict:
    """The loop-control position written to investigations.workflow_state
    at each node transition -- enough to resume an interrupted
    investigation at the right pass with the right agents."""
    snapshot = {
        "current_node": node,
        "retry_count": state.get("retry_count", 0),
        "max_retries": MAX_RETRIES,
        "agents_to_run": list(state.get("agents_to_run") or []),
        "agents_completed": list(state.get("agents_completed") or []),
        "follow_up_query": state.get("follow_up_query"),
        "validation_notes": list(state.get("validation_notes") or []),
    }
    snapshot.update(extra)
    return snapshot


def _record_transition(
    state: InvestigationState,
    node: str,
    *,
    investigation_id: Optional[str] = None,
    status: Optional[InvestigationStatus] = None,
    final_root_cause: Optional[str] = None,
    **extra: Any,
) -> None:
    """Persists the workflow's position (and optionally status/root
    cause) to Postgres. Called on entry to and exit from every node so
    the row always reflects where the investigation actually is, rather
    than jumping from 'started' to 'finished'."""
    resolved_id = investigation_id or state.get("investigation_id")
    if not resolved_id:
        return
    update_investigation(
        resolved_id,
        status=status,
        final_root_cause=final_root_cause,
        workflow_state=_workflow_snapshot(state, node, **extra),
    )


def _select_new_evidence(
    state: InvestigationState, candidates: list[EvidenceEntry]
) -> list[EvidenceEntry]:
    """Filters out evidence already recorded on an earlier pass of the
    loop, keyed on (source, finding). Without this, an agent re-run by a
    retry would re-append its previous findings through the additive
    reducer and inflate both the state and the Postgres row with
    duplicates."""
    seen = {(item["source"], item["finding"]) for item in state.get("evidence") or []}
    fresh: list[EvidenceEntry] = []
    for entry in candidates:
        key = (entry["source"], entry["finding"])
        if key in seen:
            continue
        seen.add(key)
        fresh.append(entry)
    return fresh


def _persist_evidence(
    state: InvestigationState, node: str, entries: list[EvidenceEntry]
) -> None:
    for entry in entries:
        update_investigation(state["investigation_id"], add_evidence=entry)
    _record_transition(state, node, evidence_added=len(entries))


def _should_run(state: InvestigationState, agent: str) -> bool:
    """Whether this pass scheduled the given specialist. A retry runs
    only the agents targeted at the gap validation found, so the others
    no-op rather than re-deriving what they already contributed."""
    return agent in (state.get("agents_to_run") or [])


def manager_node(state: InvestigationState) -> dict:
    """Entry point and retry coordinator.

    On the first pass: creates a new investigation row (or resumes one
    the caller already created), schedules every specialist agent, and
    marks the investigation INVESTIGATING.

    On a retry pass (validation_node routed back here because the top
    hypothesis didn't hold up): schedules a *targeted* subset instead --
    lineage_agent re-run against a query refocused on what validation
    couldn't confirm, plus the one specialist best placed to close that
    specific gap. Re-running all three would mostly re-derive evidence
    already on file.
    """
    validation = state.get("validation")
    is_retry = validation is not None

    if is_retry:
        retry_count = state.get("retry_count", 0) + 1
        plan = RETRY_PLAN.get(validation["gap"], DEFAULT_RETRY_PLAN)
        agents_to_run = ["lineage_agent"]
        if plan["agent"] != "lineage_agent":
            agents_to_run.append(plan["agent"])

        agents_completed = list(
            dict.fromkeys(
                [*(state.get("agents_completed") or []), *(state.get("agents_to_run") or [])]
            )
        )
        follow_up_query = f"{state['issue_description']} {plan['focus']}"

        update = {
            "retry_count": retry_count,
            "agents_to_run": agents_to_run,
            "agents_completed": agents_completed,
            "follow_up_query": follow_up_query,
            # Cleared so the specialists of this pass are judged on the
            # hypothesis this pass produces, not the refuted one.
            "validation": None,
        }
        _record_transition(
            {**state, **update},
            "manager",
            status=InvestigationStatus.INVESTIGATING,
            retry_reason=validation["note"],
            retry_targets=agents_to_run,
        )
        return update

    if state.get("investigation_id"):
        investigation = get_investigation(state["investigation_id"])
        if investigation is None:
            raise ValueError(
                f"No investigation found with id={state['investigation_id']!r}"
            )
    else:
        investigation = create_investigation(state["issue_description"])

    update = {
        "investigation_id": str(investigation.id),
        "status": InvestigationStatus.INVESTIGATING.value,
        "agents_to_run": list(AVAILABLE_AGENTS),
        "agents_completed": [],
        "retry_count": 0,
        "follow_up_query": None,
    }
    _record_transition(
        {**state, **update},
        "manager",
        investigation_id=str(investigation.id),
        status=InvestigationStatus.INVESTIGATING,
    )
    return update


def lineage_agent_node(state: InvestigationState) -> dict:
    """Uses the Step 4 retriever to find which SQL models/tables are
    relevant, records that as "lineage" evidence, and hands the specific
    models/tables off to sql_analysis_node and data_quality_node.

    On a retry it searches with manager_node's refocused follow-up query
    rather than the raw issue description, so it surfaces context the
    earlier pass missed instead of the same top hits."""
    if not _should_run(state, "lineage_agent"):
        return {}

    _record_transition(state, "lineage_agent")
    query = state.get("follow_up_query") or state["issue_description"]
    hits = retrieve(query, n_results=5)

    candidates: list[EvidenceEntry] = []
    relevant_sql_models: list[RelevantSqlModel] = []
    relevant_tables: list[str] = list(state.get("relevant_tables") or [])

    for hit in hits:
        metadata = hit["metadata"]
        candidates.append(
            EvidenceEntry(
                source="lineage",
                finding=" ".join(hit["document"].split()),
                confidence=round(1.0 / (1.0 + hit["distance"]), 4),
            )
        )

        table_name = metadata.get("table_name") or metadata.get("source_table")
        if table_name and table_name not in relevant_tables:
            relevant_tables.append(table_name)

        if metadata.get("type") == "sql_model":
            relevant_sql_models.append(
                RelevantSqlModel(
                    file_path=metadata["file_path"],
                    table_name=metadata.get("table_name", ""),
                    sql_text=hit["document"],
                )
            )

    # If the query didn't happen to surface any SQL models in its top-k,
    # fall back to a type-filtered search so sql_analysis_node always
    # has something concrete to review.
    if not relevant_sql_models:
        for hit in retrieve(query, filter_type="sql_model", n_results=2):
            metadata = hit["metadata"]
            relevant_sql_models.append(
                RelevantSqlModel(
                    file_path=metadata["file_path"],
                    table_name=metadata.get("table_name", ""),
                    sql_text=hit["document"],
                )
            )
            if metadata.get("table_name") and metadata["table_name"] not in relevant_tables:
                relevant_tables.append(metadata["table_name"])

    evidence = _select_new_evidence(state, candidates)
    _persist_evidence(state, "lineage_agent", evidence)

    return {
        "evidence": evidence,
        "relevant_sql_models": relevant_sql_models,
        "relevant_tables": relevant_tables,
    }


def sql_analysis_node(state: InvestigationState) -> dict:
    """Asks an LLM to review each SQL model lineage_agent_node flagged
    as relevant for obvious bugs (bad joins, missing filters), and
    records each review as "sql_analysis" evidence."""
    if not _should_run(state, "sql_analysis"):
        return {}

    _record_transition(state, "sql_analysis")
    reviewer = get_sql_reviewer()
    candidates: list[EvidenceEntry] = []
    for model in state.get("relevant_sql_models") or []:
        result = reviewer(state["issue_description"], model["table_name"], model["sql_text"])
        candidates.append(
            EvidenceEntry(
                source="sql_analysis",
                finding=f"[{model['file_path']}] {result['finding']}",
                confidence=result["confidence"],
            )
        )

    evidence = _select_new_evidence(state, candidates)
    _persist_evidence(state, "sql_analysis", evidence)
    return {"evidence": evidence}


def data_quality_node(state: InvestigationState) -> dict:
    """Runs direct row-count/duplicate-id/null-count checks against
    each table lineage_agent_node flagged as relevant, and records the
    results as "data_quality" evidence."""
    if not _should_run(state, "data_quality"):
        return {}

    _record_transition(state, "data_quality")
    candidates: list[EvidenceEntry] = []
    for table_name in state.get("relevant_tables") or []:
        try:
            report = run_basic_checks(table_name)
        except ValueError:
            continue  # not a real sandbox table (e.g. an unmaterialized mart)

        has_anomaly = bool(report["duplicate_id_count"] or report["null_counts"])
        finding = (
            f"{table_name}: {report['row_count']} row(s); "
            f"{report['duplicate_id_column'] or 'no id column'} duplicated "
            f"{report['duplicate_id_count']} time(s); "
            f"null counts: {report['null_counts'] or 'none'}"
        )
        candidates.append(
            EvidenceEntry(
                source="data_quality",
                finding=finding,
                confidence=0.6 if has_anomaly else 0.3,
            )
        )

    evidence = _select_new_evidence(state, candidates)
    _persist_evidence(state, "data_quality", evidence)
    return {"evidence": evidence}


def root_cause_node(state: InvestigationState) -> dict:
    """Takes all evidence collected so far and asks the LLM for 1-3
    ranked root-cause hypotheses.

    On a retry the notes from every previous validation attempt are
    passed along too, so the model knows which explanations have already
    been checked and ruled out rather than confidently re-proposing
    one. Only hypotheses not already on file are appended; whatever this
    pass considers most likely becomes `top_hypothesis`, which is what
    validation_node and human_review_node act on.
    """
    _record_transition(state, "root_cause")
    generate = get_root_cause_generator()
    proposed = generate(
        state["issue_description"],
        state.get("evidence") or [],
        state.get("validation_notes") or [],
    )
    if not proposed:
        return {"top_hypothesis": None}

    top_hypothesis = max(proposed, key=lambda item: item["confidence_score"])

    known = {item["description"] for item in state.get("hypotheses") or []}
    new_hypotheses: list[Hypothesis] = []
    for hypothesis in proposed:
        if hypothesis["description"] in known:
            continue
        known.add(hypothesis["description"])
        new_hypotheses.append(hypothesis)
        update_investigation(state["investigation_id"], add_hypothesis=hypothesis)

    _record_transition(
        state,
        "root_cause",
        hypotheses_added=len(new_hypotheses),
        top_hypothesis_confidence=top_hypothesis["confidence_score"],
    )
    return {"hypotheses": new_hypotheses, "top_hypothesis": top_hypothesis}


def validation_node(state: InvestigationState) -> dict:
    """Re-checks the top hypothesis against the sandbox warehouse
    directly -- re-reading the SQL models, re-reading pipeline_jobs.json,
    re-executing the models against the live schema, or re-querying the
    tables, depending on what the hypothesis is claiming (see
    app/graph/validation.py).

    The outcome is recorded as "validation" evidence so the trail shows
    what was independently verified versus merely asserted, and the note
    is kept in `validation_notes` for manager_node to retry against and
    for root_cause_node to avoid re-proposing.
    """
    _record_transition(state, "validation")
    top_hypothesis = state.get("top_hypothesis")
    outcome: ValidationOutcome = validate_hypothesis(top_hypothesis)

    entry = EvidenceEntry(
        source="validation",
        finding=f"[{outcome['claim_kind']}] {outcome['checked']}. {outcome['note']}",
        # A direct re-check against the warehouse is strong evidence
        # either way; the confidence here reflects how much weight the
        # check itself carries, not how likely the hypothesis is.
        confidence=0.9 if outcome["confirmed"] else 0.7,
    )
    evidence = _select_new_evidence(state, [entry])
    _persist_evidence(state, "validation", evidence)

    notes: list[str] = []
    if not outcome["confirmed"]:
        claimed = top_hypothesis["description"] if top_hypothesis else "(no hypothesis)"
        notes.append(f"{claimed} -> {outcome['note']}")

    _record_transition(
        {**state, "validation": outcome},
        "validation",
        validation_confirmed=outcome["confirmed"],
        validation_gap=outcome["gap"],
    )
    return {"evidence": evidence, "validation": outcome, "validation_notes": notes}


def human_review_node(state: InvestigationState) -> dict:
    """Terminal node. Closes the investigation as RESOLVED only when the
    top hypothesis clears RESOLVE_CONFIDENCE_THRESHOLD; otherwise marks
    it NEEDS_HUMAN_REVIEW and leaves final_root_cause unset rather than
    asserting a conclusion the evidence doesn't support.

    A hypothesis sitting exactly on the threshold is treated as not
    clearing it, on the same don't-fabricate-certainty principle.
    """
    _record_transition(state, "human_review")
    top_hypothesis = state.get("top_hypothesis")
    confidence = top_hypothesis["confidence_score"] if top_hypothesis else 0.0
    validation = state.get("validation")
    confirmed = bool(validation and validation["confirmed"])

    if confidence > RESOLVE_CONFIDENCE_THRESHOLD:
        status = InvestigationStatus.RESOLVED
        final_root_cause = top_hypothesis["description"]
    else:
        status = InvestigationStatus.NEEDS_HUMAN_REVIEW
        final_root_cause = None

    _record_transition(
        state,
        "human_review",
        status=status,
        final_root_cause=final_root_cause,
        outcome=status.value,
        top_hypothesis_confidence=confidence,
        validation_confirmed=confirmed,
        retries_used=state.get("retry_count", 0),
    )
    return {"status": status.value, "final_root_cause": final_root_cause}
