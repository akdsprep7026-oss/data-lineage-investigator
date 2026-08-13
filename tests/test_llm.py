"""Tests for provider selection and retry-with-backoff in app/graph/llm.py.

No network calls: the "model" here is a stub that raises whatever we
tell it to, and sleeping is patched out so the backoff schedule can be
asserted without the tests actually waiting for it.
"""

from __future__ import annotations

import pytest

from app.graph import llm as llm_module
from app.graph.llm import (
    MAX_ATTEMPTS,
    LLMUnavailable,
    coerce_llm_confidence,
    invoke_structured,
    llm_enabled,
    resolve_provider,
)


class StubLLM:
    """Raises the given exceptions in order, then returns `result`."""

    def __init__(self, errors=(), result="ok"):
        self.errors = list(errors)
        self.result = result
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


@pytest.fixture
def no_sleeping(monkeypatch):
    """Records the backoff delays instead of waiting them out."""
    slept: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", slept.append)
    return slept


def rate_limited(message="429 RESOURCE_EXHAUSTED: rate limit exceeded"):
    return RuntimeError(message)


def test_no_provider_is_used_when_no_key_is_configured():
    assert resolve_provider() is None
    assert llm_enabled() is False


def test_gemini_is_the_default_provider(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    assert resolve_provider() == "gemini"


def test_groq_is_selected_when_requested(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")
    assert resolve_provider() == "groq"


def test_requesting_groq_without_its_key_falls_back_to_gemini(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    assert resolve_provider() == "gemini"


def test_an_unedited_placeholder_key_counts_as_unconfigured(monkeypatch):
    """A .env copied from .env.example has literal placeholders in it;
    treating those as real keys would produce a wall of 401s instead of
    a clean fall-through."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "your-groq-api-key-here")
    assert resolve_provider() is None


def test_an_unknown_provider_name_falls_back_rather_than_failing(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemeni")  # typo
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    assert resolve_provider() == "gemini"


def test_a_transient_failure_is_retried_and_then_succeeds(no_sleeping):
    stub = StubLLM(errors=[rate_limited()], result="recovered")

    assert invoke_structured(stub, "prompt", purpose="test") == "recovered"
    assert stub.calls == 2
    assert no_sleeping == [2.0]


def test_backoff_grows_exponentially_between_attempts(no_sleeping):
    stub = StubLLM(errors=[rate_limited(), rate_limited()], result="recovered")

    assert invoke_structured(stub, "prompt", purpose="test") == "recovered"
    assert stub.calls == 3
    assert no_sleeping == [2.0, 4.0]


def test_a_server_supplied_retry_delay_is_honoured(no_sleeping):
    stub = StubLLM(
        errors=[rate_limited("429 RESOURCE_EXHAUSTED. Please retry in 12.5s")],
        result="recovered",
    )

    invoke_structured(stub, "prompt", purpose="test")

    assert no_sleeping == [12.5]


def test_backoff_is_capped(no_sleeping):
    stub = StubLLM(
        errors=[rate_limited("429 rate limit, please retry in 600s")],
        result="recovered",
    )

    invoke_structured(stub, "prompt", purpose="test")

    assert no_sleeping == [llm_module.MAX_BACKOFF_SECONDS]


def test_exhausted_retries_raise_llm_unavailable_for_the_caller_to_absorb(no_sleeping):
    stub = StubLLM(errors=[rate_limited() for _ in range(MAX_ATTEMPTS)])

    with pytest.raises(LLMUnavailable):
        invoke_structured(stub, "prompt", purpose="test")

    assert stub.calls == MAX_ATTEMPTS
    # One sleep fewer than attempts -- no point waiting after the last try.
    assert len(no_sleeping) == MAX_ATTEMPTS - 1


def test_a_bad_api_key_is_not_retried(no_sleeping):
    stub = StubLLM(errors=[RuntimeError("401 UNAUTHENTICATED: invalid api key")])

    with pytest.raises(LLMUnavailable):
        invoke_structured(stub, "prompt", purpose="test")

    assert stub.calls == 1
    assert no_sleeping == []


def test_an_unexpected_error_propagates_instead_of_being_absorbed(no_sleeping):
    """Retry handling must not turn a genuine bug into "the model was
    busy" -- only recognised transport failures get absorbed."""
    stub = StubLLM(errors=[ValueError("schema field 'confidence' is not a float")])

    with pytest.raises(ValueError):
        invoke_structured(stub, "prompt", purpose="test")

    assert stub.calls == 1
    assert no_sleeping == []


def test_sql_review_degrades_to_its_heuristic_when_the_model_is_unreachable(
    monkeypatch, no_sleeping
):
    """The whole point of the change: a rate-limited call costs the
    investigation accuracy on one step, not the entire run."""
    from app.graph import sql_review

    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setattr(
        sql_review,
        "build_structured_llm",
        lambda schema: StubLLM(errors=[rate_limited() for _ in range(MAX_ATTEMPTS)]),
    )

    reviewer = sql_review.get_sql_reviewer()
    assert reviewer is sql_review._llm_review_sql

    result = reviewer(
        "revenue is undercounted",
        "stg_orders_cleaned",
        "SELECT * FROM raw_orders o INNER JOIN raw_customers c ON o.customer_id = c.customer_id",
    )

    assert "INNER JOIN" in result["finding"]
    assert 0.0 <= result["confidence"] <= 1.0


def test_root_cause_degrades_to_its_heuristic_when_the_model_is_unreachable(
    monkeypatch, no_sleeping
):
    from app.graph import root_cause

    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setattr(
        root_cause,
        "build_structured_llm",
        lambda schema: StubLLM(errors=[rate_limited() for _ in range(MAX_ATTEMPTS)]),
    )

    generate = root_cause.get_root_cause_generator()
    assert generate is root_cause._llm_generate_hypotheses

    hypotheses = generate(
        "revenue is undercounted",
        [{"source": "lineage", "finding": "stg_orders_cleaned model", "confidence": 0.8}],
        [],
    )

    assert hypotheses
    assert all(0.0 <= item["confidence_score"] <= 1.0 for item in hypotheses)
    assert all(item["claim_kind"] == "unknown" for item in hypotheses)
    assert all(item["artifact"] is None for item in hypotheses)


def test_root_cause_maps_structured_claim_kind_and_artifact(monkeypatch):
    """Stubbed LLM response populates claim_kind/artifact on Hypothesis."""
    from app.graph import root_cause
    from app.graph.validation import resolve_claim_kind, validate_hypothesis
    from app.sandbox_data.incidents import incident_01_join_bug

    schema_result = root_cause._RootCauseSchema(
        hypotheses=[
            root_cause._HypothesisItem(
                claim_kind="join",
                artifact="sql_models/01_stg_orders_cleaned.sql",
                failure_mode="INNER JOIN drops unmatched customer orders",
                description=(
                    "WHERE status='completed' wording that would keyword-classify "
                    "as unknown, but the structured kind is join."
                ),
                supporting_evidence=["sql_analysis"],
                confidence_score=0.88,
            )
        ]
    )

    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setattr(
        root_cause,
        "build_structured_llm",
        lambda schema: StubLLM(result=schema_result),
    )

    hypotheses = root_cause._llm_generate_hypotheses(
        "revenue undercounted",
        [{"source": "sql_analysis", "finding": "INNER JOIN concern", "confidence": 0.7}],
        [],
    )
    assert len(hypotheses) == 1
    top = hypotheses[0]
    assert top["claim_kind"] == "join"
    assert top["artifact"] == "sql_models/01_stg_orders_cleaned.sql"
    assert top["confidence_score"] == 0.88
    assert resolve_claim_kind(top) == "join"

    incident_01_join_bug.apply()
    outcome = validate_hypothesis(top)
    assert outcome["claim_kind"] == "join"
    assert outcome["confirmed"] is True


def test_coerce_llm_confidence_accepts_numeric_forms():
    assert coerce_llm_confidence(0.8) == 0.8
    assert coerce_llm_confidence(1) == 1.0
    assert coerce_llm_confidence("0.8") == 0.8
    assert coerce_llm_confidence(" 1.0 ") == 1.0
    with pytest.raises(ValueError):
        coerce_llm_confidence("high")
    with pytest.raises(ValueError):
        coerce_llm_confidence(True)


def test_sql_review_schema_accepts_numeric_string_confidence():
    from app.graph.sql_review import _SqlReviewSchema

    parsed = _SqlReviewSchema.model_validate(
        {"finding": "possible INNER JOIN issue", "confidence": "0.8"}
    )
    assert parsed.confidence == 0.8
    assert isinstance(parsed.confidence, float)

    parsed_int = _SqlReviewSchema.model_validate(
        {"finding": "possible INNER JOIN issue", "confidence": 1}
    )
    assert parsed_int.confidence == 1.0

    parsed_float = _SqlReviewSchema.model_validate(
        {"finding": "possible INNER JOIN issue", "confidence": 0.55}
    )
    assert parsed_float.confidence == 0.55

    with pytest.raises(Exception):
        _SqlReviewSchema.model_validate(
            {"finding": "x", "confidence": "high"}
        )

    schema = _SqlReviewSchema.model_json_schema()
    confidence_schema = schema["properties"]["confidence"]
    assert "anyOf" in confidence_schema
    types = {item.get("type") for item in confidence_schema["anyOf"]}
    assert types == {"number", "string"}


def test_root_cause_schema_accepts_numeric_string_confidence():
    from app.graph.root_cause import _HypothesisItem

    item = _HypothesisItem.model_validate(
        {
            "claim_kind": "join",
            "artifact": "sql_models/01_stg_orders_cleaned.sql",
            "failure_mode": "INNER JOIN drops rows",
            "description": "join drops rows",
            "supporting_evidence": ["sql_analysis"],
            "confidence_score": "0.9",
        }
    )
    assert item.confidence_score == 0.9
    assert isinstance(item.confidence_score, float)


def test_tool_schema_mismatch_is_degradable_to_llm_unavailable(monkeypatch, no_sleeping):
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    stub = StubLLM(
        errors=[
            RuntimeError(
                "Error code: 400 - tool call validation failed: "
                "parameters for tool _SqlReviewSchema did not match schema "
                "(tool_use_failed / failed_generation)"
            )
        ]
    )
    with pytest.raises(LLMUnavailable):
        invoke_structured(stub, "prompt", purpose="SQL review")
    assert stub.calls == 1
