"""LLM-based review of a single SQL model's text for obvious bugs --
bad joins, missing filters, etc. -- used by sql_analysis_node.

Mirrors the GOOGLE_API_KEY fallback pattern used elsewhere in this
project (see app/graph/root_cause.py, app/retrieval/embeddings.py): if
GOOGLE_API_KEY is configured, a real Gemini call reviews the SQL -- this
is what production/staging should always do. Otherwise we fall back to
a crude pattern-matching heuristic so app.graph.workflow and its tests
stay runnable, fast, and offline on a fresh checkout with no API key
configured.
"""

from __future__ import annotations

import os
import re
from typing import Callable, TypedDict

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

GEMINI_MODEL = "gemini-flash-latest"


class SqlReviewResult(TypedDict):
    finding: str
    confidence: float


# (issue_description, table_name, sql_text) -> SqlReviewResult
SqlReviewer = Callable[[str, str, str], SqlReviewResult]


class _SqlReviewSchema(BaseModel):
    finding: str = Field(
        description="A specific description of any bug found in the SQL "
        "(wrong join type, missing/incorrect filter, wrong de-dup key, "
        "etc.), or a short note that no obvious issue was found."
    )
    confidence: float = Field(
        description="Confidence, from 0.0 to 1.0, that the finding above "
        "explains the reported issue."
    )


def _llm_review_sql(issue_description: str, table_name: str, sql_text: str) -> SqlReviewResult:
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL, temperature=0
    ).with_structured_output(_SqlReviewSchema)
    prompt = (
        "You are reviewing a SQL data pipeline model for bugs that could "
        "explain a reported data issue. Look specifically for things "
        "like: an INNER JOIN that should be a LEFT JOIN (silently "
        "dropping unmatched rows), a missing or incorrect WHERE filter, "
        "or a de-duplication key that wouldn't actually catch the "
        "duplicates in question.\n\n"
        f"Reported issue: {issue_description}\n\n"
        f"SQL model (builds table `{table_name}`):\n{sql_text}"
    )
    result: _SqlReviewSchema = llm.invoke(prompt)
    return SqlReviewResult(
        finding=result.finding,
        confidence=max(0.0, min(1.0, result.confidence)),
    )


_INNER_JOIN_RE = re.compile(r"\bINNER\s+JOIN\b", re.IGNORECASE)


def _heuristic_review_sql(issue_description: str, table_name: str, sql_text: str) -> SqlReviewResult:
    """Offline fallback used when GOOGLE_API_KEY isn't configured: a
    crude pattern match for one common anti-pattern (INNER JOIN, which
    silently drops unmatched rows), rather than real reasoning about the
    SQL. Good enough to exercise the graph end to end without a network
    call."""
    if _INNER_JOIN_RE.search(sql_text):
        return SqlReviewResult(
            finding=(
                f"{table_name}'s SQL uses an INNER JOIN, which silently "
                "drops rows with no match on the join key -- worth "
                "confirming a LEFT JOIN wasn't intended."
            ),
            confidence=0.4,
        )
    return SqlReviewResult(
        finding=f"No obvious join/filter issues spotted in {table_name}'s SQL (heuristic scan only).",
        confidence=0.2,
    )


def get_sql_reviewer() -> SqlReviewer:
    """Returns the Gemini-backed reviewer if GOOGLE_API_KEY is
    configured, else the offline heuristic fallback."""
    if os.getenv("GOOGLE_API_KEY"):
        return _llm_review_sql
    return _heuristic_review_sql
