"""Builds and runs the Step 5 investigation LangGraph.

A single, linear sequence -- no loops or retries yet (a future step
will add a validation_node that can send the investigation back for
another round of evidence gathering). Graph shape::

    START -> manager -> lineage_agent -> sql_analysis -> data_quality
           -> root_cause -> END

Node order after `manager` follows the numbered list in the Step 5
spec: lineage_agent_node runs first since sql_analysis_node and
data_quality_node both depend on the SQL models/tables it finds;
root_cause_node runs last since it needs all evidence gathered by the
other three.

See app/graph/nodes.py for what each node does and app/graph/state.py
for the shared state schema threaded through them.
"""

from __future__ import annotations

from typing import Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graph import nodes
from app.graph.state import InvestigationState


def build_graph() -> CompiledStateGraph:
    """Constructs and compiles the (currently linear) investigation
    graph. Compiled with an in-memory checkpointer so a run's state can
    be inspected within a process; the durable system of record is
    still the Postgres `investigations` row each node writes to via
    app/db/investigations.py.
    """
    graph: StateGraph = StateGraph(InvestigationState)

    graph.add_node("manager", nodes.manager_node)
    graph.add_node("lineage_agent", nodes.lineage_agent_node)
    graph.add_node("sql_analysis", nodes.sql_analysis_node)
    graph.add_node("data_quality", nodes.data_quality_node)
    graph.add_node("root_cause", nodes.root_cause_node)

    graph.add_edge(START, "manager")
    graph.add_edge("manager", "lineage_agent")
    graph.add_edge("lineage_agent", "sql_analysis")
    graph.add_edge("sql_analysis", "data_quality")
    graph.add_edge("data_quality", "root_cause")
    graph.add_edge("root_cause", END)

    return graph.compile(checkpointer=MemorySaver())


def run_investigation(
    issue_description: str,
    *,
    investigation_id: Optional[str] = None,
) -> InvestigationState:
    """Runs a full investigation through the linear graph end to end
    and returns the final state.

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
    }
    config = {"configurable": {"thread_id": investigation_id or issue_description}}
    return graph.invoke(initial_state, config=config)
