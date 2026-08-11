"""Builds and runs the investigation LangGraph.

As of Step 6 the graph is cyclic. Evidence gathering and root-cause
analysis are followed by a validation step that re-checks the top
hypothesis against the sandbox warehouse directly; a hypothesis that
doesn't hold up sends the investigation back to the manager for another,
narrower round of evidence gathering. As of Step 7 there are five
specialist agents instead of three::

    START -> manager -> lineage_agent -> sql_analysis -> data_quality
           -> etl_agent -> schema_agent
           -> root_cause -> validation --(not supported, retries left)--> manager
                                       \\--(supported, or retries spent)--> human_review -> END

Node order within a pass: lineage_agent always runs first because every
other specialist depends on the models/tables it finds; the four
specialists in between don't depend on each other (each reads only what
lineage_agent produced), so their relative order doesn't affect the
result -- they just run one after another rather than in parallel, for
the same reason the Step 5 graph did: it keeps the state updates simple
(no merge logic needed between concurrent branches) and the execution
trace linear and easy to follow. root_cause runs last because it needs
everything the other four gathered.

Which specialists actually do anything on a given pass varies:
manager_node's _select_agents_for_issue (see app/graph/nodes.py) scores
the issue description against keywords to schedule only the specialists
relevant to the kind of problem being reported on the first pass, and a
retry pass schedules a further-targeted subset aimed at the specific gap
validation reported. Whichever specialists aren't scheduled a given pass
fall through as no-ops. The graph shape stays the same on every pass;
only the work inside it narrows.

See app/graph/nodes.py for what each node does, app/graph/validation.py
for the direct re-checks behind validation_node, and app/graph/state.py
for the shared state schema threaded through them.
"""

from __future__ import annotations

import logging
from typing import Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.db.investigations import create_investigation
from app.db.models import InvestigationStatus
from app.graph import nodes
from app.graph.nodes import (
    MAX_RETRIES,
    MAX_VALIDATION_PASSES,
    WELL_SUPPORTED_CONFIDENCE,
    human_review_node,
)
from app.graph.state import InvestigationState
from app.graph.tracing import investigation_trace

logger = logging.getLogger(__name__)

# Worst case the graph visits 8 nodes per pass (manager, lineage_agent,
# sql_analysis, data_quality, etl_agent, schema_agent, root_cause,
# validation) across 1 initial pass plus MAX_RETRIES retries, then
# human_review -- 25 with today's MAX_RETRIES=2. The default limit of 25
# would only just accommodate that, so it's raised to leave headroom if
# MAX_RETRIES is ever increased.
RECURSION_LIMIT = 50

_TERMINAL_STATUSES = {
    InvestigationStatus.RESOLVED.value,
    InvestigationStatus.NEEDS_HUMAN_REVIEW.value,
}


def route_after_validation(state: InvestigationState) -> str:
    """Conditional edge out of validation_node.

    Sends the investigation back to manager_node for more evidence when
    the top hypothesis isn't well supported -- either the direct
    re-check contradicted it (or couldn't tie it to a concrete
    artifact), or the confidence behind it is too weak to act on. Once
    the retry budget is spent -- or validation has already run
    MAX_VALIDATION_PASSES times -- it goes to human_review_node
    regardless, which is what stops the cycle from running forever: a
    hypothesis that never gets confirmed ends up flagged for a human
    rather than retried indefinitely.
    """
    validation = state.get("validation")
    top_hypothesis = state.get("top_hypothesis")
    confidence = top_hypothesis["confidence_score"] if top_hypothesis else 0.0
    retry_count = state.get("retry_count", 0)
    validation_pass_count = state.get("validation_pass_count", 0)

    well_supported = bool(
        validation
        and validation["confirmed"]
        and confidence >= WELL_SUPPORTED_CONFIDENCE
    )
    retries_exhausted = retry_count >= MAX_RETRIES
    validation_cap_hit = validation_pass_count >= MAX_VALIDATION_PASSES

    if well_supported or retries_exhausted or validation_cap_hit:
        next_node = "human_review"
        reason = (
            "well_supported"
            if well_supported
            else (
                "retries_exhausted"
                if retries_exhausted
                else "validation_pass_cap"
            )
        )
    else:
        next_node = "manager"
        reason = "retry_for_more_evidence"

    logger.info(
        "route_after_validation id=%s decision=%s reason=%s "
        "retry_count=%s/%s validation_pass_count=%s/%s confirmed=%s "
        "confidence=%s",
        state.get("investigation_id"),
        next_node,
        reason,
        retry_count,
        MAX_RETRIES,
        validation_pass_count,
        MAX_VALIDATION_PASSES,
        bool(validation and validation.get("confirmed")),
        confidence,
    )
    return next_node


def build_graph() -> CompiledStateGraph:
    """Constructs and compiles the cyclic investigation graph. Compiled
    with an in-memory checkpointer so a run's state can be inspected
    within a process; the durable system of record is still the Postgres
    `investigations` row, which every node writes to on entry and exit
    via app/db/investigations.py.
    """
    graph: StateGraph = StateGraph(InvestigationState)

    graph.add_node("manager", nodes.manager_node)
    graph.add_node("lineage_agent", nodes.lineage_agent_node)
    graph.add_node("sql_analysis", nodes.sql_analysis_node)
    graph.add_node("data_quality", nodes.data_quality_node)
    graph.add_node("etl_agent", nodes.etl_agent_node)
    graph.add_node("schema_agent", nodes.schema_agent_node)
    graph.add_node("root_cause", nodes.root_cause_node)
    graph.add_node("validation", nodes.validation_node)
    graph.add_node("human_review", nodes.human_review_node)

    graph.add_edge(START, "manager")
    graph.add_edge("manager", "lineage_agent")
    graph.add_edge("lineage_agent", "sql_analysis")
    graph.add_edge("sql_analysis", "data_quality")
    graph.add_edge("data_quality", "etl_agent")
    graph.add_edge("etl_agent", "schema_agent")
    graph.add_edge("schema_agent", "root_cause")
    graph.add_edge("root_cause", "validation")
    graph.add_conditional_edges(
        "validation",
        route_after_validation,
        {"manager": "manager", "human_review": "human_review"},
    )
    graph.add_edge("human_review", END)

    return graph.compile(checkpointer=MemorySaver())


def _ensure_terminal_status(state: InvestigationState) -> InvestigationState:
    """Safety net: every successful graph return must leave a terminal
    status. If a path somehow ends without human_review_node, force it
    so the row never stays stuck in investigating after invoke returns.
    """
    status = state.get("status")
    if status in _TERMINAL_STATUSES:
        return state

    logger.error(
        "Graph returned non-terminal status=%s for id=%s "
        "(retry_count=%s validation_pass_count=%s); forcing human_review",
        status,
        state.get("investigation_id"),
        state.get("retry_count", 0),
        state.get("validation_pass_count", 0),
    )
    hr_update = human_review_node(state)
    return {**state, **hr_update}


def run_investigation(
    issue_description: str,
    *,
    investigation_id: Optional[str] = None,
) -> InvestigationState:
    """Runs a full investigation through the graph end to end -- however
    many retry passes it takes, up to MAX_RETRIES -- and returns the
    final state.

    Pass `investigation_id` to resume an investigation that was already
    created (e.g. via app.db.investigations.create_investigation)
    instead of starting a new one.

    The investigation id is resolved before the graph starts so Langfuse
    can use it as the session/trace id for the whole run -- including
    the first manager pass and every retry loop -- rather than only
    after the first node creates the Postgres row.
    """
    if investigation_id is None:
        investigation_id = str(create_investigation(issue_description).id)

    graph = build_graph()
    initial_state: InvestigationState = {
        "investigation_id": investigation_id,
        "issue_description": issue_description,
        "status": "pending",
        "evidence": [],
        "hypotheses": [],
        "final_root_cause": None,
        "agents_to_run": [],
        "relevant_sql_models": [],
        "relevant_tables": [],
        "retry_count": 0,
        "validation_pass_count": 0,
        "agents_completed": [],
        "follow_up_query": None,
        "top_hypothesis": None,
        "validation": None,
        "validation_notes": [],
    }
    config = {
        "configurable": {"thread_id": investigation_id},
        "recursion_limit": RECURSION_LIMIT,
    }

    with investigation_trace(investigation_id, issue_description) as root:
        final_state = graph.invoke(initial_state, config=config)
        final_state = _ensure_terminal_status(final_state)
        logger.info(
            "run_investigation complete id=%s status=%s retry_count=%s "
            "validation_pass_count=%s",
            final_state.get("investigation_id"),
            final_state.get("status"),
            final_state.get("retry_count", 0),
            final_state.get("validation_pass_count", 0),
        )
        if root is not None:
            root.update(
                output={
                    "investigation_id": final_state.get("investigation_id"),
                    "status": final_state.get("status"),
                    "retry_count": final_state.get("retry_count", 0),
                    "validation_pass_count": final_state.get(
                        "validation_pass_count", 0
                    ),
                    "evidence_count": len(final_state.get("evidence") or []),
                    "hypotheses_count": len(final_state.get("hypotheses") or []),
                    "final_root_cause": final_state.get("final_root_cause"),
                    "validation": final_state.get("validation"),
                }
            )
        return final_state
