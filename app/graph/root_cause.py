"""Root-cause hypothesis synthesis from all evidence gathered by the
specialist nodes (lineage_agent_node, sql_analysis_node,
data_quality_node), used by root_cause_node.

Mirrors the GOOGLE_API_KEY fallback pattern used elsewhere in this
project (see app/graph/sql_review.py, app/retrieval/embeddings.py): if
GOOGLE_API_KEY is configured, a real Gemini call reasons over the
evidence to produce ranked hypotheses -- this is what production/
staging should always do. Otherwise we fall back to a deterministic
heuristic (surface the top few most-relevant pieces of evidence,
uncritically) so app.graph.workflow and its tests stay runnable, fast,
and offline on a fresh checkout with no API key configured.
"""

from __future__ import annotations

import os
from typing import Callable

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from app.graph.state import EvidenceEntry, Hypothesis

load_dotenv()

GEMINI_MODEL = "gemini-flash-latest"

MAX_HYPOTHESES = 3

# The heuristic fallback doesn't actually reason about the evidence, so
# its hypotheses are capped at a middling confidence.
HEURISTIC_CONFIDENCE_CAP = 0.6

# (issue_description, evidence, refuted_notes) -> ranked hypotheses.
# `refuted_notes` carries the explanations previous passes of the graph
# already checked against the warehouse and ruled out (see
# app/graph/validation.py), so a retry proposes something genuinely new
# instead of restating a claim that has already failed verification.
RootCauseGenerator = Callable[[str, list[EvidenceEntry], list[str]], list[Hypothesis]]


class _HypothesisItem(BaseModel):
    description: str = Field(
        description="A specific, concrete explanation of the root cause, "
        "naming the exact table/column/job/SQL file responsible."
    )
    supporting_evidence: list[str] = Field(
        description="The 'source' field of each evidence item that "
        "supports this explanation (e.g. 'lineage', 'sql_analysis', "
        "'data_quality')."
    )
    confidence_score: float = Field(
        description="Confidence in this hypothesis, from 0.0 (pure "
        "guess) to 1.0 (certain), given only the evidence gathered so far."
    )


class _RootCauseSchema(BaseModel):
    hypotheses: list[_HypothesisItem] = Field(
        description="1 to 3 candidate root-cause explanations, ranked "
        "most-likely first.",
        min_length=1,
        max_length=MAX_HYPOTHESES,
    )


def _format_evidence(evidence: list[EvidenceEntry]) -> str:
    if not evidence:
        return "(no evidence gathered)"
    return "\n".join(
        f"- [{item['source']}] (confidence={item['confidence']:.2f}): {item['finding']}"
        for item in evidence
    )


def _format_refuted(refuted_notes: list[str]) -> str:
    if not refuted_notes:
        return ""
    ruled_out = "\n".join(f"- {note}" for note in refuted_notes)
    return (
        "\n\nThese explanations were proposed on an earlier pass and then "
        "checked directly against the warehouse, and did NOT hold up. Do "
        "not propose them again; account for why the check came back the "
        "way it did and look elsewhere:\n"
        f"{ruled_out}"
    )


def _llm_generate_hypotheses(
    issue_description: str,
    evidence: list[EvidenceEntry],
    refuted_notes: list[str] | None = None,
) -> list[Hypothesis]:
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL, temperature=0
    ).with_structured_output(_RootCauseSchema)
    prompt = (
        "You are a data lineage investigator diagnosing a data quality "
        "incident in a pipeline. Given the reported issue and the "
        "evidence gathered so far by three specialist agents -- lineage "
        "(which SQL models/tables are relevant), sql_analysis (an LLM "
        "review of those models' SQL for bugs), and data_quality (direct "
        "row-count/duplicate/null checks on the relevant tables) -- "
        "propose 1 to 3 ranked root-cause hypotheses, most-likely first. "
        "Be specific: name the exact table, column, SQL file, or job you "
        "believe is responsible.\n\n"
        f"Issue: {issue_description}\n\n"
        f"Evidence gathered so far:\n{_format_evidence(evidence)}"
        f"{_format_refuted(refuted_notes or [])}"
    )
    result: _RootCauseSchema = llm.invoke(prompt)
    hypotheses = [
        Hypothesis(
            description=item.description,
            supporting_evidence=item.supporting_evidence,
            confidence_score=max(0.0, min(1.0, item.confidence_score)),
        )
        for item in result.hypotheses
    ]
    return sorted(hypotheses, key=lambda h: h["confidence_score"], reverse=True)


def _heuristic_generate_hypotheses(
    issue_description: str,
    evidence: list[EvidenceEntry],
    refuted_notes: list[str] | None = None,
) -> list[Hypothesis]:
    """Offline fallback used when GOOGLE_API_KEY isn't configured: just
    surfaces the top few most-relevant pieces of evidence gathered so
    far as hypotheses, rather than actually reasoning about them. Good
    enough to exercise the graph end to end without a network call.

    It can't reason about `refuted_notes`, so it settles for not
    re-proposing evidence a previous pass already had refuted -- which
    is also what makes a retry surface the next-best candidate instead
    of looping on the same one."""
    refuted_text = " ".join(refuted_notes or [])
    candidates = [
        item
        for item in evidence
        # A validation finding is a verdict on a hypothesis, not a
        # candidate root cause in its own right.
        if item["source"] != "validation" and item["finding"] not in refuted_text
    ]
    evidence = candidates or evidence
    if not evidence:
        return [
            Hypothesis(
                description=f"Not enough evidence gathered to explain: {issue_description}",
                supporting_evidence=[],
                confidence_score=0.0,
            )
        ]
    ranked_evidence = sorted(evidence, key=lambda item: item["confidence"], reverse=True)
    return [
        Hypothesis(
            description=f"Possibly related to [{item['source']}]: {item['finding']}",
            supporting_evidence=[item["source"]],
            confidence_score=min(item["confidence"], HEURISTIC_CONFIDENCE_CAP),
        )
        for item in ranked_evidence[:MAX_HYPOTHESES]
    ]


def get_root_cause_generator() -> RootCauseGenerator:
    """Returns the Gemini-backed generator if GOOGLE_API_KEY is
    configured, else the offline heuristic fallback."""
    if os.getenv("GOOGLE_API_KEY"):
        return _llm_generate_hypotheses
    return _heuristic_generate_hypotheses
