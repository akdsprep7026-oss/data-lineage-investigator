"""Builds and runs the investigation LangGraph.

As of Step 6 the graph is cyclic. Evidence gathering and root-cause
analysis are followed by a validation step that re-checks the top
hypothesis against the sandbox warehouse directly; a hypothesis that
doesn't hold up sends the investigation back to the manager for another,
narrower round of evidence gathering::

    START -> manager -> lineage_agent -> sql_analysis -> data_quality
           -> root_cause -> validation --(not supported, retries left)--> manager
                                       \\--(supported, or retries spent)--> human_review -> END

Node order within a pass is unchanged from the linear Step 5 graph:
lineage_agent runs first because sql_analysis and data_quality both
depend on the models/tables it finds, and root_cause runs last because
it needs everything the other three gathered.

What differs on a retry pass is *which* of those nodes actually do
anything: manager_node schedules a targeted subset of specialists aimed
at the specific gap validation reported, and the unscheduled ones fall
through as no-ops (see app/graph/nodes.py). The graph shape stays the
same on every pass; only the work inside it narrows.

See app/graph/nodes.py for what each node does, app/graph/validation.py
for the direct re-checks behind validation_node, and app/graph/state.py
for the shared state schema threaded through them.
"""

from __future__ import annotations

from typing import Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graph import nodes
from app.graph.nodes import MAX_RETRIES, WELL_SUPPORTED_CONFIDENCE
from app.graph.state import InvestigationState

# Worst case the graph visits 6 nodes per pass across 1 initial pass plus
# MAX_RETRIES retries, then human_review. The default limit of 25 would
# only just accommodate that, so it's raised to leave headroom if
# MAX_RETRIES is ever increased.
RECURSION_LIMIT = 50


def route_after_validation(state: InvestigationState) -> str:
    """Conditional edge out of validation_node.

    Sends the investigation back to manager_node for more evidence when
    the top hypothesis isn't well supported -- either the direct
    re-check contradicted it (or couldn't tie it to a concrete
    artifact), or the confidence behind it is too weak to act on. Once
    the retry budget is spent, it goes to human_review_node regardless,
    which is what stops the cycle from running forever: a hypothesis
    that never gets confirmed ends up flagged for a human rather than
    retried indefinitely.
    """
    validation = state.get("validation")
    top_hypothesis = state.get("top_hypothesis")
    confidence = top_hypothesis["confidence_score"] if top_hypothesis else 0.0

    well_supported = bool(
        validation
        and validation["confirmed"]
        and confidence >= WELL_SUPPORTED_CONFIDENCE
    )
    if well_supported or state.get("retry_count", 0) >= MAX_RETRIES:
        return "human_review"
    return "manager"


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
    graph.add_node("root_cause", nodes.root_cause_node)
    graph.add_node("validation", nodes.validation_node)
    graph.add_node("human_review", nodes.human_review_node)

    graph.add_edge(START, "manager")
    graph.add_edge("manager", "lineage_agent")
    graph.add_edge("lineage_agent", "sql_analysis")
    graph.add_edge("sql_analysis", "data_quality")
    graph.add_edge("data_quality", "root_cause")
    graph.add_edge("root_cause", "validation")
    graph.add_conditional_edges(
        "validation",
        route_after_validation,
        {"manager": "manager", "human_review": "human_review"},
    )
    graph.add_edge("human_review", END)

    return graph.compile(checkpointer=MemorySaver())


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
    """
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
        "agents_completed": [],
        "follow_up_query": None,
        "top_hypothesis": None,
        "validation": None,
        "validation_notes": [],
    }
    config = {
        "configurable": {"thread_id": investigation_id or issue_description},
        "recursion_limit": RECURSION_LIMIT,
    }
    return graph.invoke(initial_state, config=config)
