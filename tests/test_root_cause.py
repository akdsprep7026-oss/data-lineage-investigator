"""Focused unit tests for root-cause claim classification (no live LLM)."""

from __future__ import annotations

from app.graph.root_cause import (
    HEURISTIC_CONFIDENCE_CAP,
    _HypothesisItem,
    _finalize_hypothesis,
    _heuristic_generate_hypotheses,
    build_root_cause_prompt,
    prepare_evidence_for_synthesis,
)
from app.graph.validation import resolve_claim_kind, validate_hypothesis


def _item(**kwargs) -> _HypothesisItem:
    defaults = {
        "claim_kind": "unknown",
        "artifact": None,
        "failure_mode": "unclear",
        "description": "unclear",
        "supporting_evidence": ["lineage"],
        "confidence_score": 0.5,
    }
    defaults.update(kwargs)
    return _HypothesisItem(**defaults)


def test_prepare_evidence_prefers_diagnostic_sources_and_truncates():
    evidence = [
        {
            "source": "lineage",
            "finding": "SELECT " + ("x" * 500) + " FROM raw_orders INNER JOIN raw_customers",
            "confidence": 0.95,
        },
        {
            "source": "validation",
            "finding": "prior verdict",
            "confidence": 0.99,
        },
        {
            "source": "etl_agent",
            "finding": "build_fct_daily_revenue: last_run_status='failed'",
            "confidence": 0.75,
        },
        {
            "source": "schema_agent",
            "finding": "[sql_models/01_stg_orders_cleaned.sql] references o.created_at missing",
            "confidence": 0.8,
        },
        {
            "source": "data_quality",
            "finding": "raw_orders: 2 group(s) share customer_id/amount/day under new ids",
            "confidence": 0.75,
        },
    ]
    prepared = prepare_evidence_for_synthesis(evidence)
    sources = [item["source"] for item in prepared]
    assert "validation" not in sources
    assert sources[0] == "schema_agent"
    assert sources[1] == "etl_agent"
    assert sources[2] == "data_quality"
    assert sources[-1] == "lineage"
    assert len(prepared[-1]["finding"]) < len(evidence[0]["finding"])
    assert prepared[-1]["finding"].endswith("...")


def test_prompt_lists_claim_kinds_and_decision_order():
    prompt = build_root_cause_prompt(
        "revenue looks wrong",
        [
            {
                "source": "etl_agent",
                "finding": "build_fct_daily_revenue failed",
                "confidence": 0.75,
            }
        ],
    )
    assert "Choose claim_kind" in prompt
    assert "stale_pipeline" in prompt
    assert "schema_change" in prompt
    assert "Do NOT confuse" in prompt
    assert "schema mismatch wins" in prompt.lower() or "Prefer this over stale_pipeline" in prompt


def test_finalize_join_requires_artifact():
    hyp = _finalize_hypothesis(
        _item(
            claim_kind="join",
            artifact="sql_models/01_stg_orders_cleaned.sql",
            failure_mode="INNER JOIN drops unmatched rows",
            description="Orders from new customers are dropped",
            supporting_evidence=["sql_analysis"],
            confidence_score=0.9,
        )
    )
    assert hyp["claim_kind"] == "join"
    assert hyp["artifact"] == "sql_models/01_stg_orders_cleaned.sql"
    assert hyp["confidence_score"] == 0.9
    assert resolve_claim_kind(hyp) == "join"


def test_finalize_stale_pipeline():
    hyp = _finalize_hypothesis(
        _item(
            claim_kind="stale_pipeline",
            artifact="build_fct_daily_revenue",
            failure_mode="Job failed leaving fct stale",
            description="build_fct_daily_revenue failed for two days",
            supporting_evidence=["etl_agent"],
            confidence_score=0.9,
        )
    )
    assert hyp["claim_kind"] == "stale_pipeline"
    assert hyp["artifact"] == "build_fct_daily_revenue"


def test_finalize_schema_change():
    hyp = _finalize_hypothesis(
        _item(
            claim_kind="schema_change",
            artifact="sql_models/01_stg_orders_cleaned.sql",
            failure_mode="SQL still references renamed created_at",
            description="o.created_at no longer exists; renamed to order_created_at",
            supporting_evidence=["schema_agent"],
            confidence_score=0.9,
        )
    )
    assert hyp["claim_kind"] == "schema_change"
    assert "created_at" in hyp["description"]


def test_finalize_duplicates():
    hyp = _finalize_hypothesis(
        _item(
            claim_kind="duplicates",
            artifact="raw_orders",
            failure_mode="Same transaction re-emitted under new order_ids",
            description="Duplicate transactions inflate revenue",
            supporting_evidence=["data_quality"],
            confidence_score=0.85,
        )
    )
    assert hyp["claim_kind"] == "duplicates"
    assert hyp["artifact"] == "raw_orders"


def test_checkable_kind_without_artifact_becomes_unknown():
    hyp = _finalize_hypothesis(
        _item(
            claim_kind="join",
            artifact=None,
            failure_mode="maybe a join",
            description="something drops rows",
            confidence_score=0.95,
        )
    )
    assert hyp["claim_kind"] == "unknown"
    assert hyp["artifact"] is None
    assert hyp["confidence_score"] <= HEURISTIC_CONFIDENCE_CAP


def test_ambiguous_unknown_stays_conservative():
    hyp = _finalize_hypothesis(
        _item(
            claim_kind="unknown",
            artifact="sql_models/02_fct_daily_revenue.sql",
            failure_mode="unclear filter behavior",
            description="status filter might matter",
            confidence_score=0.95,
        )
    )
    assert hyp["claim_kind"] == "unknown"
    assert hyp["artifact"] is None
    assert hyp["confidence_score"] <= HEURISTIC_CONFIDENCE_CAP
    outcome = validate_hypothesis(hyp)
    assert outcome["confirmed"] is False
    assert outcome["gap"] == "unclassifiable_claim"


def test_signal_backed_schema_change():
    from app.graph.root_cause import signal_backed_hypotheses

    hyps = signal_backed_hypotheses(
        "job failing",
        [
            {
                "source": "schema_agent",
                "finding": (
                    "[app/sandbox_data/sql_models/01_stg_orders_cleaned.sql] "
                    "references column(s) that don't exist in the live schema: "
                    "raw_orders.created_at (referenced as o.created_at)."
                ),
                "confidence": 0.8,
            },
            {
                "source": "etl_agent",
                "finding": "build_stg_orders_cleaned: last_run_status='failed' as of now",
                "confidence": 0.75,
            },
        ],
    )
    assert len(hyps) == 1
    assert hyps[0]["claim_kind"] == "schema_change"
    assert "01_stg_orders_cleaned.sql" in (hyps[0]["artifact"] or "")


def test_signal_backed_stale_pipeline():
    from app.graph.root_cause import signal_backed_hypotheses

    hyps = signal_backed_hypotheses(
        "missing days",
        [
            {
                "source": "etl_agent",
                "finding": (
                    "build_fct_daily_revenue: last_run_status='failed' as of "
                    "2024-01-28T15:15:07Z (timeout)."
                ),
                "confidence": 0.75,
            },
            {
                "source": "schema_agent",
                "finding": "[sql_models/02_fct_daily_revenue.sql] every column exists",
                "confidence": 0.2,
            },
        ],
    )
    assert hyps[0]["claim_kind"] == "stale_pipeline"
    assert hyps[0]["artifact"] == "build_fct_daily_revenue"


def test_signal_backed_duplicates_and_join():
    from app.graph.root_cause import signal_backed_hypotheses

    dups = signal_backed_hypotheses(
        "revenue high",
        [
            {
                "source": "data_quality",
                "finding": (
                    "stg_orders_cleaned: 178 row(s); order_id duplicated 0 time(s); "
                    "11 group(s) of rows share the same customer_id/amount/day under "
                    "more than one distinct order_id"
                ),
                "confidence": 0.75,
            }
        ],
    )
    assert dups[0]["claim_kind"] == "duplicates"

    joins = signal_backed_hypotheses(
        "undercount",
        [
            {
                "source": "sql_analysis",
                "finding": (
                    "[app/sandbox_data/sql_models/01_stg_orders_cleaned.sql] "
                    "INNER JOIN silently drops unmatched customer rows"
                ),
                "confidence": 0.8,
            }
        ],
    )
    assert joins[0]["claim_kind"] == "join"
    assert "01_stg_orders_cleaned.sql" in (joins[0]["artifact"] or "")


def test_signal_backed_ambiguous_returns_empty():
    from app.graph.root_cause import signal_backed_hypotheses

    assert (
        signal_backed_hypotheses(
            "weird",
            [{"source": "lineage", "finding": "dashboard widget", "confidence": 0.9}],
        )
        == []
    )


def test_heuristic_still_unknown_without_artifact():
    items = _heuristic_generate_hypotheses(
        "revenue looks wrong",
        [
            {
                "source": "lineage",
                "finding": "INNER JOIN mentioned in comments " + ("x" * 400),
                "confidence": 0.95,
            },
            {
                "source": "etl_agent",
                "finding": "build_fct_daily_revenue failed",
                "confidence": 0.75,
            },
        ],
        [],
    )
    assert items
    assert items[0]["supporting_evidence"] == ["etl_agent"]
    for item in items:
        assert item["claim_kind"] == "unknown"
        assert item["artifact"] is None
        assert item["confidence_score"] <= HEURISTIC_CONFIDENCE_CAP
