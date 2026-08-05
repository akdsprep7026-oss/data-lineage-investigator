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
