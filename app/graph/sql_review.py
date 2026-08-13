"""LLM-based review of a single SQL model's text for obvious bugs --
bad joins, missing filters, etc. -- used by sql_analysis_node.

Uses whichever provider app/graph/llm.py resolves (Gemini by default,
Groq when LLM_PROVIDER=groq) -- this is what production/staging should
always do. If no provider is configured, or a configured one can't be
reached for this particular call, we fall back to a crude
pattern-matching heuristic so app.graph.workflow and its tests stay
runnable, fast, and offline on a fresh checkout with no API key.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, TypedDict

from pydantic import BaseModel, Field, field_validator

from app.graph.llm import (
    LLMUnavailable,
    build_structured_llm,
    coerce_llm_confidence,
    invoke_structured,
    llm_enabled,
)

logger = logging.getLogger(__name__)


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
    # float | str so the tool JSON schema is number|string: Groq sometimes
    # emits \"0.8\" and rejects a number-only schema before Pydantic runs.
    # The validator always stores a real float and rejects non-numeric text.
    confidence: float | str = Field(
        description="Confidence, from 0.0 to 1.0, that the finding above "
        "explains the reported issue. Prefer a JSON number; numeric "
        "strings such as \"0.8\" are also accepted."
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> float:
        return coerce_llm_confidence(value)


def _llm_review_sql(issue_description: str, table_name: str, sql_text: str) -> SqlReviewResult:
    llm = build_structured_llm(_SqlReviewSchema)
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
    try:
        result: _SqlReviewSchema = invoke_structured(
            llm, prompt, purpose=f"SQL review of {table_name}"
        )
    except LLMUnavailable as exc:
        # One unreachable model call shouldn't sink the investigation --
        # degrade this step to the heuristic and carry on. Logged rather
        # than silent, so a run full of these is visible.
        logger.warning("%s Falling back to the heuristic SQL scan.", exc)
        return _heuristic_review_sql(issue_description, table_name, sql_text)

    return SqlReviewResult(
        finding=result.finding,
        confidence=max(0.0, min(1.0, float(result.confidence))),
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
    """Returns the LLM-backed reviewer if a provider is configured, else
    the offline heuristic fallback."""
    if llm_enabled():
        return _llm_review_sql
    return _heuristic_review_sql
