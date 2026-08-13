"""Root-cause hypothesis synthesis from all evidence gathered by the
specialist nodes (lineage_agent_node, sql_analysis_node,
data_quality_node, etl_agent_node, schema_agent_node), used by
root_cause_node.

Uses whichever provider app/graph/llm.py resolves (Gemini by default,
Groq when LLM_PROVIDER=groq) to reason over the evidence and produce
ranked hypotheses -- this is what production/staging should always do.
If no provider is configured, or a configured one can't be reached for
this particular call, we fall back to a deterministic heuristic (surface
the top few most-relevant pieces of evidence, uncritically) so
app.graph.workflow and its tests stay runnable, fast, and offline on a
fresh checkout with no API key.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from pydantic import BaseModel, Field, field_validator

from app.graph.llm import (
    LLMUnavailable,
    build_structured_llm,
    coerce_llm_confidence,
    invoke_structured,
    llm_enabled,
)
from app.graph.state import CHECKABLE_CLAIM_KINDS, ClaimKind, EvidenceEntry, Hypothesis

logger = logging.getLogger(__name__)

MAX_HYPOTHESES = 3

# The heuristic fallback doesn't actually reason about the evidence, so
# its hypotheses are capped at a middling confidence.
HEURISTIC_CONFIDENCE_CAP = 0.6

# When the LLM is unreachable, high-precision specialist signals can still
# produce a checkable claim without inventing evidence.
SIGNAL_BACKED_CONFIDENCE = 0.85

# Synthesis-only presentation limits. Persisted evidence is unchanged;
# long lineage SQL dumps were observed to overwhelm Groq structured
# output and push the graph onto the unknown heuristic path.
MAX_SYNTHESIS_EVIDENCE = 8
MAX_FINDING_CHARS = 280

# Prefer diagnostic specialist findings over raw retrieval dumps when
# building the root-cause prompt (and when ranking heuristic candidates).
_SOURCE_PRIORITY = {
    "schema_agent": 0,
    "etl_agent": 1,
    "data_quality": 2,
    "sql_analysis": 3,
    "lineage": 4,
}

_SQL_PATH_RE = re.compile(
    r"(?:app/sandbox_data/)?(sql_models/[A-Za-z0-9_./-]+\.sql)", re.IGNORECASE
)
_BRACKET_PATH_RE = re.compile(r"\[([^\]]+)\]")
_JOB_FAILED_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*last_run_status='(?:failed|error)'",
    re.IGNORECASE,
)
_TABLE_PREFIX_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")
_TX_DUP_RE = re.compile(
    r"(\d+)\s+group\(s\) of rows share the same customer_id/amount/day",
    re.IGNORECASE,
)
_ID_DUP_RE = re.compile(r"duplicated\s+(\d+)\s+time", re.IGNORECASE)

# (issue_description, evidence, refuted_notes) -> ranked hypotheses.
# `refuted_notes` carries the explanations previous passes of the graph
# already checked against the warehouse and ruled out (see
# app/graph/validation.py), so a retry proposes something genuinely new
# instead of restating a claim that has already failed verification.
RootCauseGenerator = Callable[[str, list[EvidenceEntry], list[str]], list[Hypothesis]]


class _HypothesisItem(BaseModel):
    claim_kind: ClaimKind = Field(
        description=(
            "Choose FIRST, from evidence only. One of: join, stale_pipeline, "
            "schema_change, duplicates, or unknown. Never invent a checkable "
            "kind to look resolvable."
        )
    )
    artifact: Optional[str] = Field(
        default=None,
        description=(
            "Required when claim_kind is checkable: exact SQL model path, "
            "pipeline job name, table, or column blamed "
            "(e.g. sql_models/01_stg_orders_cleaned.sql or "
            "build_fct_daily_revenue). Null only when claim_kind is unknown."
        ),
    )
    failure_mode: Optional[str] = Field(
        default=None,
        description=(
            "One concise sentence naming the mechanism (e.g. 'INNER JOIN "
            "drops unmatched customers', 'job failed leaving fact table "
            "stale', 'SQL references renamed column', 'same transaction "
            "re-emitted under new order_ids'). Optional but preferred."
        ),
    )
    description: str = Field(
        description=(
            "Concise root-cause explanation that names the blamed artifact "
            "and restates the failure mode. Do not paste raw evidence."
        )
    )
    supporting_evidence: list[str] = Field(
        description=(
            "The 'source' field of each evidence item that supports this "
            "explanation (e.g. 'schema_agent', 'etl_agent', 'sql_analysis')."
        )
    )
    confidence_score: float | str = Field(
        description=(
            "Confidence from 0.0 to 1.0 given only the evidence so far. "
            "Use <=0.6 when claim_kind is unknown. Prefer a JSON number; "
            "numeric strings such as \"0.8\" are also accepted."
        )
    )

    @field_validator("confidence_score", mode="before")
    @classmethod
    def _coerce_confidence_score(cls, value: object) -> float:
        return coerce_llm_confidence(value)


class _RootCauseSchema(BaseModel):
    hypotheses: list[_HypothesisItem] = Field(
        description="1 to 3 candidate root-cause explanations, ranked "
        "most-likely first. Classify claim_kind before writing prose.",
        min_length=1,
        max_length=MAX_HYPOTHESES,
    )


def _normalize_claim_kind(raw: str | None) -> ClaimKind:
    if raw in CHECKABLE_CLAIM_KINDS or raw == "unknown":
        return raw  # type: ignore[return-value]
    return "unknown"


def _truncate_finding(finding: str, limit: int = MAX_FINDING_CHARS) -> str:
    text = " ".join((finding or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def prepare_evidence_for_synthesis(
    evidence: list[EvidenceEntry],
) -> list[EvidenceEntry]:
    """Reorder/truncate evidence for root-cause prompting only.

    Does not mutate or remove items from persisted investigation state.
    Drops validation verdicts (not candidate causes) and prefers
    schema/etl/data_quality/sql findings over long lineage SQL dumps.
    """
    candidates = [
        item for item in evidence if item.get("source") != "validation"
    ]
    if not candidates:
        candidates = list(evidence)

    def sort_key(item: EvidenceEntry) -> tuple:
        source = item.get("source") or ""
        return (
            _SOURCE_PRIORITY.get(source, 50),
            -float(item.get("confidence") or 0.0),
        )

    ranked = sorted(candidates, key=sort_key)
    prepared: list[EvidenceEntry] = []
    for item in ranked[:MAX_SYNTHESIS_EVIDENCE]:
        prepared.append(
            EvidenceEntry(
                source=item["source"],
                finding=_truncate_finding(item["finding"]),
                confidence=item["confidence"],
            )
        )
    return prepared


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


def build_root_cause_prompt(
    issue_description: str,
    evidence: list[EvidenceEntry],
    refuted_notes: list[str] | None = None,
) -> str:
    """Evidence-first classification prompt for structured root-cause output."""
    synthesis_evidence = prepare_evidence_for_synthesis(evidence)
    return (
        "You are a data lineage investigator diagnosing a data quality "
        "incident. Specialist agents gathered evidence from lineage, "
        "sql_analysis, data_quality, etl_agent, and schema_agent. Absence "
        "of a source means 'not checked', not 'healthy'.\n\n"
        "REASON IN THIS ORDER for each hypothesis (and fill the schema "
        "fields in the same order):\n"
        "1) Read the diagnostic evidence (prefer schema_agent, etl_agent, "
        "data_quality, sql_analysis over raw SQL dumps).\n"
        "2) Choose claim_kind.\n"
        "3) Name artifact (required for checkable kinds).\n"
        "4) State failure_mode in one sentence.\n"
        "5) Write a concise description (do NOT paste evidence).\n"
        "6) List supporting_evidence sources and confidence_score.\n\n"
        "SUPPORTED claim_kind values and what distinguishes them:\n"
        "- join: SQL uses INNER JOIN (or equivalent) that drops unmatched "
        "rows; symptom is missing/undercounted rows while jobs succeed.\n"
        "- stale_pipeline: a pipeline job is failed/late and a downstream "
        "table is missing recent days, WHILE upstream schema/SQL still "
        "matches the live columns. Job failure alone is not enough if "
        "schema_agent reports a missing/renamed column that explains the "
        "failure.\n"
        "- schema_change: SQL references a column that does not exist in "
        "the live schema (rename/drop). Prefer this over stale_pipeline "
        "when schema_agent reports a mismatch — even if the job also "
        "failed.\n"
        "- duplicates: rows/transactions are counted more than once "
        "(duplicate ids OR same customer/amount/day under new ids). Do "
        "not call this a join problem.\n"
        "- unknown: only when evidence does not support any of the four "
        "above. Keep confidence <= 0.6 and artifact null.\n\n"
        "Do NOT confuse:\n"
        "- stale_pipeline with schema_change (schema mismatch wins).\n"
        "- schema_change with stale_pipeline (job failed because of the "
        "rename is still schema_change).\n"
        "- duplicates with joins.\n"
        "- joins with generic 'missing data' when no INNER JOIN / drop "
        "behavior is in evidence.\n"
        "- healthy/clean findings with a root cause (ignore low-signal "
        "'everything looks fine' items when stronger anomalies exist).\n\n"
        "Do not invent a checkable kind merely to look resolvable. Do not "
        "treat an intentional status='completed' revenue filter as a bug "
        "unless evidence shows it diverges from the business rule AND the "
        "claim maps to a checkable kind.\n\n"
        f"Issue: {issue_description}\n\n"
        f"Evidence (diagnostic-first, truncated for synthesis):\n"
        f"{_format_evidence(synthesis_evidence)}"
        f"{_format_refuted(refuted_notes or [])}"
    )


def _normalize_sql_artifact(raw: str | None) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip().replace("\\", "/")
    match = _SQL_PATH_RE.search(text)
    if match:
        return match.group(1)
    if text.endswith(".sql"):
        return text.split("/")[-1]
    return text


def _artifact_from_finding(finding: str) -> Optional[str]:
    text = finding or ""
    sql = _SQL_PATH_RE.search(text)
    if sql:
        return sql.group(1)
    # Prefer bracketed SQL paths; ignore labels like "[INCIDENT 1: join bug]".
    for match in _BRACKET_PATH_RE.finditer(text):
        inner = match.group(1).strip()
        lowered = inner.lower()
        if "incident" in lowered:
            continue
        if ".sql" in lowered or "sql_models/" in lowered:
            return _normalize_sql_artifact(inner)
    stem = re.search(r"\b([A-Za-z0-9_]+_orders_cleaned)\.sql\b", text)
    if stem:
        return f"{stem.group(1)}.sql"
    return None


def signal_backed_hypotheses(
    issue_description: str,
    evidence: list[EvidenceEntry],
    refuted_notes: list[str] | None = None,
) -> list[Hypothesis]:
    """High-precision claims from specialist findings when the LLM is down.

    Priority matches the prompt contract: schema_change beats stale_pipeline.
    Returns an empty list when no strong signal is present so callers can
    fall through to the unknown heuristic. Never invents evidence.
    """
    del issue_description  # signals come only from gathered evidence
    refuted_text = " ".join(refuted_notes or []).lower()
    prepared = prepare_evidence_for_synthesis(evidence)
    usable = [
        item
        for item in prepared
        if item["finding"].lower() not in refuted_text
        and float(item.get("confidence") or 0.0) >= 0.55
    ]

    schema_hits = [
        item
        for item in usable
        if item["source"] == "schema_agent"
        and (
            "don't exist" in item["finding"].lower()
            or "does not exist" in item["finding"].lower()
            or "do not exist" in item["finding"].lower()
        )
    ]
    if schema_hits:
        item = schema_hits[0]
        artifact = _artifact_from_finding(item["finding"])
        if not artifact:
            return []
        return [
            Hypothesis(
                description=(
                    "Upstream schema change broke the SQL model: "
                    f"{item['finding']}"
                ),
                supporting_evidence=["schema_agent"],
                confidence_score=SIGNAL_BACKED_CONFIDENCE,
                claim_kind="schema_change",
                artifact=artifact,
                failure_mode="SQL references a missing or renamed column",
            )
        ]

    etl_hits = [
        item
        for item in usable
        if item["source"] == "etl_agent"
        and _JOB_FAILED_RE.search(item["finding"] or "")
    ]
    if etl_hits:
        item = etl_hits[0]
        match = _JOB_FAILED_RE.search(item["finding"])
        artifact = match.group(1) if match else None
        if not artifact:
            return []
        return [
            Hypothesis(
                description=(
                    f"Pipeline job {artifact} failed and left downstream "
                    f"tables stale: {item['finding']}"
                ),
                supporting_evidence=["etl_agent"],
                confidence_score=SIGNAL_BACKED_CONFIDENCE,
                claim_kind="stale_pipeline",
                artifact=artifact,
                failure_mode="Failing job left the fact table stale",
            )
        ]

    for item in usable:
        if item["source"] != "data_quality":
            continue
        finding = item["finding"]
        tx = _TX_DUP_RE.search(finding)
        id_dup = _ID_DUP_RE.search(finding)
        anomalous = (tx and int(tx.group(1)) > 0) or (
            id_dup and int(id_dup.group(1)) > 0
        )
        if not anomalous:
            continue
        table_match = _TABLE_PREFIX_RE.match(finding)
        artifact = table_match.group(1) if table_match else "raw_orders"
        # Prefer the raw landing table when the finding is about re-emitted ids.
        if "raw_orders" in finding:
            artifact = "raw_orders"
        elif artifact == "stg_orders_cleaned":
            artifact = "stg_orders_cleaned"
        return [
            Hypothesis(
                description=(
                    "Duplicate transactions are inflating measures: "
                    f"{finding}"
                ),
                supporting_evidence=["data_quality"],
                confidence_score=SIGNAL_BACKED_CONFIDENCE,
                claim_kind="duplicates",
                artifact=artifact,
                failure_mode="Same transaction counted more than once",
            )
        ]

    for item in usable:
        if item["source"] not in {"sql_analysis", "lineage"}:
            continue
        lowered = item["finding"].lower()
        if "inner join" not in lowered:
            continue
        if not any(
            marker in lowered
            for marker in ("drop", "silent", "unmatched", "no match", "exclude")
        ):
            # Still accept a clear INNER JOIN callout from sql_analysis.
            if item["source"] != "sql_analysis":
                continue
        artifact = _artifact_from_finding(item["finding"])
        if not artifact and "stg_orders_cleaned" in item["finding"].lower():
            artifact = "stg_orders_cleaned"
        if not artifact:
            continue
        return [
            Hypothesis(
                description=(
                    "INNER JOIN is dropping unmatched rows: "
                    f"{item['finding']}"
                ),
                supporting_evidence=[item["source"]],
                confidence_score=SIGNAL_BACKED_CONFIDENCE,
                claim_kind="join",
                artifact=artifact,
                failure_mode="INNER JOIN drops unmatched rows",
            )
        ]

    return []


def _finalize_hypothesis(item: _HypothesisItem) -> Hypothesis:
    """Map structured LLM output onto Hypothesis; enforce artifact rule."""
    kind = _normalize_claim_kind(item.claim_kind)
    artifact = item.artifact.strip() if item.artifact else None
    failure_mode = (item.failure_mode or "").strip() or None
    confidence = max(0.0, min(1.0, float(item.confidence_score)))
    description = (item.description or "").strip()
    if failure_mode and failure_mode.lower() not in description.lower():
        description = f"{failure_mode.rstrip('.')}. {description}".strip()

    # Checkable kinds without a named artifact are not actionable; treat
    # as unknown rather than letting keyword fallback invent a kind.
    if kind in CHECKABLE_CLAIM_KINDS and not artifact:
        kind = "unknown"
        confidence = min(confidence, HEURISTIC_CONFIDENCE_CAP)
        artifact = None

    if kind == "unknown":
        confidence = min(confidence, HEURISTIC_CONFIDENCE_CAP)
        artifact = None

    hypothesis = Hypothesis(
        description=description,
        supporting_evidence=list(item.supporting_evidence or []),
        confidence_score=confidence,
        claim_kind=kind,
        artifact=artifact,
    )
    if failure_mode:
        hypothesis["failure_mode"] = failure_mode
    return hypothesis


def _llm_generate_hypotheses(
    issue_description: str,
    evidence: list[EvidenceEntry],
    refuted_notes: list[str] | None = None,
) -> list[Hypothesis]:
    llm = build_structured_llm(_RootCauseSchema)
    prompt = build_root_cause_prompt(
        issue_description, evidence, refuted_notes
    )
    try:
        result: _RootCauseSchema = invoke_structured(
            llm, prompt, purpose="root-cause synthesis"
        )
    except LLMUnavailable as exc:
        # Prefer high-precision specialist signals over the unknown
        # heuristic when the model is rate-limited or unreachable.
        logger.warning("%s Trying evidence-signal classification.", exc)
        signal_hypotheses = signal_backed_hypotheses(
            issue_description, evidence, refuted_notes
        )
        if signal_hypotheses:
            logger.warning(
                "Using signal-backed claim_kind=%s (LLM unavailable).",
                signal_hypotheses[0].get("claim_kind"),
            )
            return signal_hypotheses
        logger.warning("No strong evidence signal; falling back to heuristic ranking.")
        return _heuristic_generate_hypotheses(
            issue_description, evidence, refuted_notes
        )

    hypotheses = [_finalize_hypothesis(item) for item in result.hypotheses]
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
    of looping on the same one.

    Always emits claim_kind=unknown and artifact=None with conservative
    confidence so validation never resolves from this path alone.
    """
    refuted_text = " ".join(refuted_notes or [])
    prepared = prepare_evidence_for_synthesis(evidence)
    candidates = [
        item
        for item in prepared
        if item["finding"] not in refuted_text
    ]
    evidence_for_rank = candidates or prepared or evidence
    if not evidence_for_rank:
        return [
            Hypothesis(
                description=f"Not enough evidence gathered to explain: {issue_description}",
                supporting_evidence=[],
                confidence_score=0.0,
                claim_kind="unknown",
                artifact=None,
            )
        ]
    return [
        Hypothesis(
            description=f"Possibly related to [{item['source']}]: {item['finding']}",
            supporting_evidence=[item["source"]],
            confidence_score=min(item["confidence"], HEURISTIC_CONFIDENCE_CAP),
            claim_kind="unknown",
            artifact=None,
        )
        for item in evidence_for_rank[:MAX_HYPOTHESES]
    ]


def get_root_cause_generator() -> RootCauseGenerator:
    """Returns the LLM-backed generator if a provider is configured,
    else the offline heuristic fallback."""
    if llm_enabled():
        return _llm_generate_hypotheses
    return _heuristic_generate_hypotheses
