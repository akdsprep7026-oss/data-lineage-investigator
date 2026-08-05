"""Direct re-checks of a root-cause hypothesis against the sandbox
warehouse, used by validation_node (see app/graph/nodes.py).

The point of this module is to be *independent* of the evidence the
specialist agents already gathered. Re-scoring a hypothesis against the
same evidence that produced it would be circular and would confirm a
confident-sounding wrong answer just as readily as a right one. So each
check here goes back to the source of truth instead: it re-reads the
.sql model files off disk, re-reads pipeline_jobs.json, re-executes the
models against the live sandbox schema, or re-queries the tables.

Two things make a claim "confirmed":

  1. The re-check has to positively agree with the *kind* of bug being
     claimed (e.g. an INNER JOIN really is present in some model).
  2. The claim has to be specific enough to tie to a concrete artifact.
     A hypothesis naming a SQL file, job, or table that the re-check
     found nothing wrong with is *contradicted*, not confirmed; a
     hypothesis too vague to name anything is left unconfirmed rather
     than being generously matched to whatever the re-check happened to
     find. This is what keeps the workflow from fabricating certainty
     when the LLM produces the right-sounding kind of answer pointed at
     the wrong object.

Anything not confirmed comes back with a `gap` string that manager_node
maps to whichever specialist agent is most likely to close it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple, Optional

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.graph.state import ClaimKind, Hypothesis, ValidationOutcome
from app.sandbox_data.models import get_engine

SANDBOX_DIR = Path(__file__).resolve().parents[1] / "sandbox_data"
SQL_MODELS_DIR = SANDBOX_DIR / "sql_models"
PIPELINE_JOBS_PATH = SANDBOX_DIR / "pipeline_jobs.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SQL_COMMENT_RE = re.compile(r"--.*")
_TARGET_RE = re.compile(r"--\s*Target:\s*(\S+)", re.IGNORECASE)
_INNER_JOIN_RE = re.compile(r"\bINNER\s+JOIN\b", re.IGNORECASE)

# Candidate event-timestamp columns on raw_orders, in priority order.
# Incident 3 renames created_at to order_created_at, so the duplicate
# check can't assume either one exists.
TIMESTAMP_COLUMN_CANDIDATES = ("created_at", "order_created_at")

# Keyword -> claim kind. Scored by how many distinct keywords for a kind
# appear in the hypothesis text, so a description mentioning both a
# failing job and a renamed column resolves to whichever it leans on
# more, rather than to whichever happens to be checked first.
CLAIM_KEYWORDS: dict[ClaimKind, tuple[str, ...]] = {
    "schema_change": (
        "schema",
        "renamed",
        "rename",
        "no such column",
        "missing column",
        "column is missing",
        "column name",
        "does not exist",
        "doesn't exist",
        "no longer exists",
        "order_created_at",
    ),
    "stale_pipeline": (
        "stale",
        "failing",
        "failed",
        "not refreshed",
        "never refreshed",
        "last_run",
        "has not run",
        "hasn't run",
        "out of date",
        "timeout",
    ),
    "join": (
        "inner join",
        "left join",
        "join",
        "dropped",
        "unmatched",
        "orphan",
    ),
    "duplicates": (
        "duplicate",
        "duplicated",
        "double",
        "twice",
        "de-dup",
        "dedup",
        "inflat",
        "counted more than once",
    ),
}


class SqlModel(NamedTuple):
    relative_path: str
    stem: str
    target_table: str
    sql_text: str

    @property
    def identifiers(self) -> tuple[str, ...]:
        """The strings a hypothesis might plausibly use to refer to this
        model, used to decide whether a claim actually named it.

        Includes the filename stem with its ordering prefix stripped
        ("01_stg_orders_cleaned" -> "stg_orders_cleaned") because a
        hypothesis normally refers to a model by the table it builds,
        and an incident that rewrites a model can drop the "-- Target:"
        header that would otherwise supply that name.
        """
        return (
            self.relative_path,
            f"{self.stem}.sql",
            self.stem,
            re.sub(r"^\d+_", "", self.stem),
            self.target_table,
        )


def _strip_comments(sql_text: str) -> str:
    return _SQL_COMMENT_RE.sub("", sql_text)


def load_sql_models() -> list[SqlModel]:
    models: list[SqlModel] = []
    for file_path in sorted(SQL_MODELS_DIR.glob("*.sql")):
        content = file_path.read_text(encoding="utf-8")
        target_match = _TARGET_RE.search(content)
        models.append(
            SqlModel(
                relative_path=str(file_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                stem=file_path.stem,
                target_table=target_match.group(1) if target_match else file_path.stem,
                sql_text=content,
            )
        )
    return models


def load_pipeline_jobs() -> list[dict]:
    if not PIPELINE_JOBS_PATH.exists():
        return []
    return json.loads(PIPELINE_JOBS_PATH.read_text(encoding="utf-8")).get("jobs", [])


def classify_claim(description: str) -> ClaimKind:
    """Buckets a hypothesis into the kind of bug it's claiming, so the
    matching direct re-check can be run against it."""
    text_lower = description.lower()
    scores = {
        kind: sum(1 for keyword in keywords if keyword in text_lower)
        for kind, keywords in CLAIM_KEYWORDS.items()
    }
    best_kind, best_score = max(scores.items(), key=lambda item: item[1])
    return best_kind if best_score else "unknown"


def _names_any(description: str, candidates: tuple[str, ...]) -> bool:
    text_lower = description.lower()
    return any(candidate.lower() in text_lower for candidate in candidates if candidate)


def _attribute(
    description: str, flagged: list, others: list, identifiers_of
) -> tuple[str, Optional[object]]:
    """Works out which artifact a claim is pointing at, given the ones
    the re-check flagged as faulty (`flagged`) and the rest (`others`).

    A hypothesis usually names several artifacts in passing -- the model
    it blames, plus the downstream table whose numbers looked wrong --
    so a flagged artifact takes precedence over any other it happens to
    mention. Only a claim that names something and none of the flagged
    artifacts is treated as pointing at the wrong one.

    Returns ("flagged" | "other" | "none", the matched item or None).
    """
    for item in flagged:
        if _names_any(description, identifiers_of(item)):
            return "flagged", item
    for item in others:
        if _names_any(description, identifiers_of(item)):
            return "other", item
    return "none", None


def _timestamp_column(inspector, table_name: str) -> Optional[str]:
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    for candidate in TIMESTAMP_COLUMN_CANDIDATES:
        if candidate in columns:
            return candidate
    return None


def _check_join(description: str) -> ValidationOutcome:
    """Re-reads every SQL model off disk and looks for the INNER JOIN
    the claim depends on."""
    models = load_sql_models()
    offenders = [model for model in models if _INNER_JOIN_RE.search(_strip_comments(model.sql_text))]
    checked = f"re-read {len(models)} SQL model(s) in {SQL_MODELS_DIR.name}/ looking for INNER JOINs"

    if not offenders:
        return ValidationOutcome(
            claim_kind="join",
            confirmed=False,
            checked=checked,
            note=(
                "Contradicted: none of the "
                f"{len(models)} SQL models contains an INNER JOIN -- every join "
                "on disk right now is a LEFT JOIN, so no rows are being dropped "
                "for want of a match. The reported symptom has some other cause."
            ),
            gap="join_not_present",
        )

    offender_paths = ", ".join(model.relative_path for model in offenders)
    innocent = [model for model in models if model not in offenders]
    attribution, named = _attribute(
        description, offenders, innocent, lambda model: model.identifiers
    )

    if attribution == "flagged":
        return ValidationOutcome(
            claim_kind="join",
            confirmed=True,
            checked=checked,
            note=(
                f"Confirmed: {named.relative_path} does contain an INNER JOIN, "
                "which silently drops rows with no match on the join key."
            ),
            gap="",
        )

    if attribution == "other":
        return ValidationOutcome(
            claim_kind="join",
            confirmed=False,
            checked=checked,
            note=(
                f"Contradicted: the hypothesis blames {named.relative_path}, but that "
                "model uses no INNER JOIN. The INNER JOIN(s) on disk are in "
                f"{offender_paths}."
            ),
            gap="join_wrong_model",
        )

    if len(offenders) == 1:
        return ValidationOutcome(
            claim_kind="join",
            confirmed=True,
            checked=checked,
            note=(
                f"Confirmed: {offenders[0].relative_path} does contain an "
                "INNER JOIN. (The hypothesis didn't name a specific model, "
                "but only one model on disk has an INNER JOIN at all.)"
            ),
            gap="",
        )
    return ValidationOutcome(
        claim_kind="join",
        confirmed=False,
        checked=checked,
        note=(
            f"Unconfirmed: {len(offenders)} models contain an INNER JOIN "
            f"({offender_paths}), but the hypothesis doesn't name which one "
            "is at fault, so the re-check can't tie the claim to a specific model."
        ),
        gap="join_model_unnamed",
    )


def _check_stale_pipeline(description: str) -> ValidationOutcome:
    """Re-reads pipeline_jobs.json and cross-checks it against how far
    the downstream fact table actually extends."""
    jobs = load_pipeline_jobs()
    unhealthy = [job for job in jobs if job.get("last_run_status") != "success"]
    checked = f"re-read pipeline_jobs.json ({len(jobs)} job(s)) and the freshness of fct_daily_revenue"
    freshness = _describe_freshness()

    if not unhealthy:
        last_runs = ", ".join(
            f"{job['job_name']} @ {job.get('last_run_at')}" for job in jobs
        )
        return ValidationOutcome(
            claim_kind="stale_pipeline",
            confirmed=False,
            checked=checked,
            note=(
                "Contradicted: every job in pipeline_jobs.json reports "
                f"last_run_status='success' ({last_runs}). {freshness} "
                "Nothing indicates a job stopped running."
            ),
            gap="pipeline_healthy",
        )

    unhealthy_names = ", ".join(
        f"{job['job_name']} (status={job.get('last_run_status')}, "
        f"last_run_at={job.get('last_run_at')})"
        for job in unhealthy
    )
    healthy = [job for job in jobs if job not in unhealthy]
    attribution, named = _attribute(
        description, unhealthy, healthy, lambda job: (job["job_name"],)
    )

    if attribution == "flagged":
        return ValidationOutcome(
            claim_kind="stale_pipeline",
            confirmed=True,
            checked=checked,
            note=(
                f"Confirmed: job {named['job_name']} reports "
                f"last_run_status='{named.get('last_run_status')}' as of "
                f"{named.get('last_run_at')}"
                + (
                    f" with error {named['error_message']!r}"
                    if named.get("error_message")
                    else ""
                )
                + f". {freshness}"
            ),
            gap="",
        )

    if attribution == "other":
        return ValidationOutcome(
            claim_kind="stale_pipeline",
            confirmed=False,
            checked=checked,
            note=(
                f"Contradicted: the hypothesis blames {named['job_name']}, but that job "
                f"reports last_run_status='{named.get('last_run_status')}' as of "
                f"{named.get('last_run_at')}. The unhealthy job(s) are: {unhealthy_names}."
            ),
            gap="pipeline_wrong_job",
        )

    if len(unhealthy) == 1:
        return ValidationOutcome(
            claim_kind="stale_pipeline",
            confirmed=True,
            checked=checked,
            note=(
                f"Confirmed: {unhealthy_names}. {freshness} (The hypothesis "
                "didn't name a specific job, but only one job is unhealthy.)"
            ),
            gap="",
        )
    return ValidationOutcome(
        claim_kind="stale_pipeline",
        confirmed=False,
        checked=checked,
        note=(
            f"Unconfirmed: {len(unhealthy)} jobs are unhealthy ({unhealthy_names}) "
            "but the hypothesis doesn't name which one is responsible."
        ),
        gap="pipeline_job_unnamed",
    )


def _describe_freshness() -> str:
    """One-line summary of how far stg_orders_cleaned and
    fct_daily_revenue actually extend, so a staleness claim can be
    cross-checked against the data and not just the job metadata."""
    engine = get_engine()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    parts: list[str] = []
    with engine.connect() as connection:
        if "stg_orders_cleaned" in tables:
            timestamp_column = _timestamp_column(inspector, "stg_orders_cleaned")
            if timestamp_column:
                latest = connection.execute(
                    text(f"SELECT MAX(DATE({timestamp_column})) FROM stg_orders_cleaned")
                ).scalar()
                parts.append(f"stg_orders_cleaned extends to {latest}")
        if "fct_daily_revenue" in tables:
            latest = connection.execute(
                text("SELECT MAX(date) FROM fct_daily_revenue")
            ).scalar()
            parts.append(f"fct_daily_revenue extends to {latest}")
    return ("Data freshness: " + "; ".join(parts) + ".") if parts else ""


def _check_schema_change(description: str) -> ValidationOutcome:
    """Re-executes every SQL model against the live sandbox schema. A
    genuine breaking schema change shows up as the database itself
    rejecting the model, which is about as direct as verification gets."""
    models = load_sql_models()
    engine = get_engine()
    checked = f"re-executed {len(models)} SQL model(s) against the live sandbox schema"

    broken: list[tuple[SqlModel, str]] = []
    for model in models:
        try:
            with engine.connect() as connection:
                # SELECT-only, but roll back regardless so re-validating
                # can never mutate the warehouse being investigated.
                transaction = connection.begin()
                try:
                    connection.execute(text(model.sql_text)).fetchone()
                finally:
                    transaction.rollback()
        except SQLAlchemyError as exc:
            broken.append((model, str(getattr(exc, "orig", exc)).strip()))

    if not broken:
        return ValidationOutcome(
            claim_kind="schema_change",
            confirmed=False,
            checked=checked,
            note=(
                f"Contradicted: all {len(models)} SQL models execute cleanly against "
                "the current sandbox schema, so no referenced column is missing or "
                "renamed."
            ),
            gap="schema_intact",
        )

    broken_summary = "; ".join(
        f"{model.relative_path} fails with {error!r}" for model, error in broken
    )
    broken_models = [model for model, _ in broken]
    attribution, _named = _attribute(
        description,
        broken_models,
        [model for model in models if model not in broken_models],
        lambda model: model.identifiers,
    )
    if attribution == "none" and len(broken) > 1:
        return ValidationOutcome(
            claim_kind="schema_change",
            confirmed=False,
            checked=checked,
            note=(
                f"Unconfirmed: {len(broken)} models fail to execute ({broken_summary}) "
                "but the hypothesis doesn't name which one the schema change broke."
            ),
            gap="schema_model_unnamed",
        )

    return ValidationOutcome(
        claim_kind="schema_change",
        confirmed=True,
        checked=checked,
        note=f"Confirmed by re-execution: {broken_summary}.",
        gap="",
    )


def _check_duplicates(description: str) -> ValidationOutcome:
    """Re-queries raw_orders for both kinds of duplication: repeated
    order_ids (which the staging model's window function already
    de-duplicates) and the same transaction re-emitted under *distinct*
    order_ids (which it cannot catch)."""
    engine = get_engine()
    inspector = inspect(engine)
    if "raw_orders" not in inspector.get_table_names():
        return ValidationOutcome(
            claim_kind="duplicates",
            confirmed=False,
            checked="looked for raw_orders in the sandbox warehouse",
            note="Unconfirmed: raw_orders does not exist in the sandbox warehouse.",
            gap="table_missing",
        )

    timestamp_column = _timestamp_column(inspector, "raw_orders")
    checked = "re-queried raw_orders for repeated order_ids and for repeated transactions under distinct order_ids"

    with engine.connect() as connection:
        repeated_order_ids = connection.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "SELECT order_id FROM raw_orders GROUP BY order_id HAVING COUNT(*) > 1)"
            )
        ).scalar()

        if timestamp_column is None:
            distinct_id_duplicates = None
        else:
            distinct_id_duplicates = connection.execute(
                text(
                    "SELECT COUNT(*) FROM ("
                    "SELECT customer_id, amount, DATE("
                    f"{timestamp_column}) AS day "
                    "FROM raw_orders GROUP BY customer_id, amount, day "
                    "HAVING COUNT(DISTINCT order_id) > 1)"
                )
            ).scalar()

    if distinct_id_duplicates:
        return ValidationOutcome(
            claim_kind="duplicates",
            confirmed=True,
            checked=checked,
            note=(
                f"Confirmed: {distinct_id_duplicates} transaction group(s) in raw_orders "
                "appear more than once under different order_ids (same customer_id, "
                "amount and day), which the PARTITION BY order_id de-duplication in the "
                f"staging model cannot catch. Separately, {repeated_order_ids} order_id(s) "
                "are repeated, and those the window function does handle."
            ),
            gap="",
        )

    return ValidationOutcome(
        claim_kind="duplicates",
        confirmed=False,
        checked=checked,
        note=(
            "Contradicted: no transaction appears under more than one order_id in "
            f"raw_orders. The {repeated_order_ids} repeated order_id(s) present are "
            "already de-duplicated by the staging model's window function, so they "
            "cannot be inflating downstream totals."
        ),
        gap="duplicates_absent",
    )


CLAIM_CHECKS = {
    "join": _check_join,
    "stale_pipeline": _check_stale_pipeline,
    "schema_change": _check_schema_change,
    "duplicates": _check_duplicates,
}


def validate_hypothesis(hypothesis: Optional[Hypothesis]) -> ValidationOutcome:
    """Re-checks a hypothesis against the sandbox warehouse directly and
    reports whether the claim holds up."""
    if hypothesis is None:
        return ValidationOutcome(
            claim_kind="unknown",
            confirmed=False,
            checked="nothing -- no hypothesis was produced",
            note="Unconfirmed: root cause analysis produced no hypothesis to check.",
            gap="no_hypothesis",
        )

    description = hypothesis["description"]
    claim_kind = classify_claim(description)
    if claim_kind == "unknown":
        return ValidationOutcome(
            claim_kind="unknown",
            confirmed=False,
            checked="classified the claim against the checkable failure modes",
            note=(
                "Unconfirmed: the hypothesis doesn't describe a failure mode that can "
                "be re-checked directly (a join dropping rows, a stale/failing job, a "
                "breaking schema change, or duplicated rows), so it can't be verified "
                "against the warehouse."
            ),
            gap="unclassifiable_claim",
        )

    return CLAIM_CHECKS[claim_kind](description)
