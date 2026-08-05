"""Node functions for the Step 5 investigation LangGraph (see
app/graph/workflow.py for how these are wired into a single linear
sequence -- no loops or retries yet, that's Step 6).

Each node returns a partial InvestigationState update for LangGraph to
merge (see app/graph/state.py for the reducers governing how evidence/
hypotheses accumulate), and persists its findings to the
`investigations` table in Postgres via app/db/investigations.py, so the
investigation survives a process restart and can be resumed by id.
"""

from __future__ import annotations

from app.db.investigations import (
    create_investigation,
    get_investigation,
    update_investigation,
)
from app.db.models import InvestigationStatus
from app.graph.data_quality import run_basic_checks
from app.graph.root_cause import get_root_cause_generator
from app.graph.sql_review import get_sql_reviewer
from app.graph.state import EvidenceEntry, InvestigationState, RelevantSqlModel
from app.retrieval.retriever import retrieve

# Every specialist agent the manager can currently dispatch. There's no
# branching logic yet -- manager_node always schedules all of them --
# but naming them here is what a future step can condition on once the
# manager is able to selectively skip agents.
AVAILABLE_AGENTS = ["lineage_agent", "sql_analysis", "data_quality"]


def manager_node(state: InvestigationState) -> dict:
    """Creates a new investigation row, or resumes an existing one if
    the caller already set `investigation_id`; decides which specialist
    agents to run (today: always all of them); marks the investigation
    INVESTIGATING."""
    if state.get("investigation_id"):
        investigation = get_investigation(state["investigation_id"])
        if investigation is None:
            raise ValueError(
                f"No investigation found with id={state['investigation_id']!r}"
            )
    else:
        investigation = create_investigation(state["issue_description"])

    update_investigation(investigation.id, status=InvestigationStatus.INVESTIGATING)

    return {
        "investigation_id": str(investigation.id),
        "status": InvestigationStatus.INVESTIGATING.value,
        "agents_to_run": list(AVAILABLE_AGENTS),
    }


def lineage_agent_node(state: InvestigationState) -> dict:
    """Uses the Step 4 retriever to find which SQL models/tables are
    relevant to the issue, records that as "lineage" evidence, and hands
    off the specific SQL models/tables found to sql_analysis_node and
    data_quality_node."""
    hits = retrieve(state["issue_description"], n_results=5)

    evidence: list[EvidenceEntry] = []
    relevant_sql_models: list[RelevantSqlModel] = []
    relevant_tables: list[str] = []

    for hit in hits:
        metadata = hit["metadata"]
        entry = EvidenceEntry(
            source="lineage",
            finding=" ".join(hit["document"].split()),
            confidence=round(1.0 / (1.0 + hit["distance"]), 4),
        )
        evidence.append(entry)
        update_investigation(state["investigation_id"], add_evidence=entry)

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

    # If the general query didn't happen to surface any SQL models in
    # its top-k, fall back to a type-filtered search so sql_analysis_node
    # always has *something* concrete to review.
    if not relevant_sql_models:
        for hit in retrieve(state["issue_description"], filter_type="sql_model", n_results=2):
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

    return {
        "evidence": evidence,
        "relevant_sql_models": relevant_sql_models,
        "relevant_tables": relevant_tables,
    }


def sql_analysis_node(state: InvestigationState) -> dict:
    """Asks an LLM to review each SQL model lineage_agent_node flagged
    as relevant for obvious bugs (bad joins, missing filters), and
    records each review as "sql_analysis" evidence."""
    reviewer = get_sql_reviewer()
    evidence: list[EvidenceEntry] = []
    for model in state["relevant_sql_models"]:
        result = reviewer(state["issue_description"], model["table_name"], model["sql_text"])
        entry = EvidenceEntry(
            source="sql_analysis",
            finding=f"[{model['file_path']}] {result['finding']}",
            confidence=result["confidence"],
        )
        evidence.append(entry)
        update_investigation(state["investigation_id"], add_evidence=entry)

    return {"evidence": evidence}


def data_quality_node(state: InvestigationState) -> dict:
    """Runs direct row-count/duplicate-id/null-count checks against
    each table lineage_agent_node flagged as relevant, and records the
    results as "data_quality" evidence."""
    evidence: list[EvidenceEntry] = []
    for table_name in state["relevant_tables"]:
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
        entry = EvidenceEntry(
            source="data_quality",
            finding=finding,
            confidence=0.6 if has_anomaly else 0.3,
        )
        evidence.append(entry)
        update_investigation(state["investigation_id"], add_evidence=entry)

    return {"evidence": evidence}


def root_cause_node(state: InvestigationState) -> dict:
    """Takes all evidence collected by the specialist nodes so far and
    asks the LLM to produce 1-3 ranked root-cause hypotheses with
    confidence scores, persisting each to the investigation record."""
    generate = get_root_cause_generator()
    hypotheses = generate(state["issue_description"], state["evidence"])
    for hypothesis in hypotheses:
        update_investigation(state["investigation_id"], add_hypothesis=hypothesis)
    return {"hypotheses": hypotheses}
