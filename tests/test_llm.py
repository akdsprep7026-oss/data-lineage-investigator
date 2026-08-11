"""Tests for provider selection, retry-with-backoff, and Gemini→Groq
failover in app/graph/llm.py.

No network calls: the "model" here is a stub that raises whatever we
tell it to, and sleeping is patched out so the backoff schedule can be
asserted without the tests actually waiting for it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

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

    def invoke(self, prompt, config=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


class _TinySchema(BaseModel):
    value: str = Field(description="a value")


@pytest.fixture
def no_sleeping(monkeypatch):
    """Records the backoff delays instead of waiting them out."""
    slept: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", slept.append)
    return slept


def rate_limited(message="429 RESOURCE_EXHAUSTED: rate limit exceeded"):
    return RuntimeError(message)


def quota_exhausted(
    message=(
        "429 RESOURCE_EXHAUSTED Quota exceeded for: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    ),
):
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


def test_hard_quota_exhaustion_fails_fast_without_retries(no_sleeping):
    stub = StubLLM(errors=[quota_exhausted()])

    with pytest.raises(LLMUnavailable, match="quota exhausted"):
        invoke_structured(stub, "prompt", purpose="test")

    assert stub.calls == 1
    assert no_sleeping == []


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


def test_gemini_success_does_not_call_groq(monkeypatch, no_sleeping):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")

    gemini = StubLLM(result=SimpleNamespace(value="from-gemini"))
    groq = StubLLM(result=SimpleNamespace(value="from-groq"))

    result = invoke_structured(
        gemini,
        "prompt",
        purpose="root-cause synthesis",
        schema=_TinySchema,
        secondary_llm=groq,
    )

    assert result.value == "from-gemini"
    assert gemini.calls == 1
    assert groq.calls == 0


def test_gemini_transient_recovery_does_not_call_groq(monkeypatch, no_sleeping):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")

    gemini = StubLLM(errors=[rate_limited()], result=SimpleNamespace(value="recovered"))
    groq = StubLLM(result=SimpleNamespace(value="from-groq"))

    result = invoke_structured(
        gemini,
        "prompt",
        purpose="root-cause synthesis",
        schema=_TinySchema,
        secondary_llm=groq,
    )

    assert result.value == "recovered"
    assert gemini.calls == 2
    assert groq.calls == 0


def test_gemini_quota_exhaustion_fails_over_to_groq(monkeypatch, no_sleeping):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")

    gemini = StubLLM(errors=[quota_exhausted()])
    groq = StubLLM(result=SimpleNamespace(value="from-groq"))

    result = invoke_structured(
        gemini,
        "prompt",
        purpose="root-cause synthesis",
        schema=_TinySchema,
        secondary_llm=groq,
    )

    assert result.value == "from-groq"
    assert gemini.calls == 1
    assert groq.calls == 1
    assert no_sleeping == []


def test_gemini_and_groq_unavailable_raises_llm_unavailable(monkeypatch, no_sleeping):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")

    gemini = StubLLM(errors=[quota_exhausted()])
    groq = StubLLM(errors=[rate_limited() for _ in range(MAX_ATTEMPTS)])

    with pytest.raises(LLMUnavailable):
        invoke_structured(
            gemini,
            "prompt",
            purpose="root-cause synthesis",
            schema=_TinySchema,
            secondary_llm=groq,
        )

    assert gemini.calls == 1
    assert groq.calls == MAX_ATTEMPTS


def test_manual_groq_mode_never_calls_gemini(monkeypatch, no_sleeping):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")

    groq = StubLLM(result=SimpleNamespace(value="from-groq"))
    gemini = StubLLM(result=SimpleNamespace(value="from-gemini"))

    result = invoke_structured(
        groq,
        "prompt",
        purpose="root-cause synthesis",
        schema=_TinySchema,
        secondary_llm=gemini,
    )

    assert result.value == "from-groq"
    assert groq.calls == 1
    assert gemini.calls == 0


def test_gemini_failure_without_groq_key_raises_without_secondary(
    monkeypatch, no_sleeping
):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    gemini = StubLLM(errors=[quota_exhausted()])

    with pytest.raises(LLMUnavailable):
        invoke_structured(
            gemini,
            "prompt",
            purpose="root-cause synthesis",
            schema=_TinySchema,
        )

    assert gemini.calls == 1


def test_gemini_auth_failure_fails_over_to_groq(monkeypatch, no_sleeping):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")

    gemini = StubLLM(errors=[RuntimeError("401 UNAUTHENTICATED: invalid api key")])
    groq = StubLLM(result=SimpleNamespace(value="from-groq"))

    result = invoke_structured(
        gemini,
        "prompt",
        purpose="root-cause synthesis",
        schema=_TinySchema,
        secondary_llm=groq,
    )

    assert result.value == "from-groq"
    assert gemini.calls == 1
    assert groq.calls == 1
    assert no_sleeping == []


def test_groq_fallback_preserves_structured_schema(monkeypatch, no_sleeping):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")

    structured = _TinySchema(value="structured-ok")
    gemini = StubLLM(errors=[quota_exhausted()])
    groq = StubLLM(result=structured)

    result = invoke_structured(
        gemini,
        "prompt",
        purpose="root-cause synthesis",
        schema=_TinySchema,
        secondary_llm=groq,
    )

    assert isinstance(result, _TinySchema)
    assert result.value == "structured-ok"


def test_sql_review_degrades_to_its_heuristic_when_the_model_is_unreachable(
    monkeypatch, no_sleeping
):
    """The whole point of the change: a rate-limited call costs the
    investigation accuracy on one step, not the entire run."""
    from app.graph import sql_review

    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
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
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
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


def test_root_cause_uses_heuristic_when_both_providers_unavailable(
    monkeypatch, no_sleeping
):
    from app.graph import root_cause

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")
    monkeypatch.setattr(
        root_cause,
        "build_structured_llm",
        lambda schema: StubLLM(errors=[quota_exhausted()]),
    )
    monkeypatch.setattr(
        llm_module,
        "build_structured_llm_for_provider",
        lambda provider, schema: StubLLM(
            errors=[rate_limited() for _ in range(MAX_ATTEMPTS)]
        ),
    )

    generate = root_cause.get_root_cause_generator()
    hypotheses = generate(
        "revenue is undercounted",
        [{"source": "lineage", "finding": "stg_orders_cleaned model", "confidence": 0.8}],
        [],
    )

    assert hypotheses
    assert hypotheses[0]["description"].startswith("Possibly related to")


def test_production_failover_constructs_groq_via_build_structured_llm_for_provider(
    monkeypatch, no_sleeping
):
    """Gemini failure must go through the real secondary-construction path,
    not only the secondary_llm= test hook."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")

    gemini = StubLLM(errors=[quota_exhausted()])
    groq = StubLLM(result=SimpleNamespace(value="from-constructed-groq"))
    construction_calls: list[tuple[str, type]] = []

    def fake_build(provider, schema):
        construction_calls.append((provider, schema))
        assert provider == "groq"
        assert schema is _TinySchema
        return groq

    monkeypatch.setattr(
        llm_module, "build_structured_llm_for_provider", fake_build
    )

    result = invoke_structured(
        gemini,
        "prompt",
        purpose="root-cause synthesis",
        schema=_TinySchema,
    )

    assert result.value == "from-constructed-groq"
    assert gemini.calls == 1
    assert groq.calls == 1
    assert construction_calls == [("groq", _TinySchema)]
    assert no_sleeping == []


def test_manual_groq_failure_does_not_fall_back_to_gemini(monkeypatch, no_sleeping):
    """LLM_PROVIDER=groq means Groq-only, even when a Gemini key exists."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")

    groq = StubLLM(errors=[rate_limited() for _ in range(MAX_ATTEMPTS)])
    construction_calls: list[tuple[str, type]] = []

    def fake_build(provider, schema):
        construction_calls.append((provider, schema))
        raise AssertionError(
            f"must not construct secondary provider {provider!r} in groq mode"
        )

    monkeypatch.setattr(
        llm_module, "build_structured_llm_for_provider", fake_build
    )

    with pytest.raises(LLMUnavailable):
        invoke_structured(
            groq,
            "prompt",
            purpose="root-cause synthesis",
            schema=_TinySchema,
        )

    assert groq.calls == MAX_ATTEMPTS
    assert construction_calls == []


def test_omitted_schema_does_not_construct_secondary_provider(
    monkeypatch, no_sleeping
):
    """Documents the current contract: without schema=, automatic Groq
    construction is not attempted even when both keys are configured."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real_groq_key")

    gemini = StubLLM(errors=[quota_exhausted()])
    construction_calls: list[tuple[str, type]] = []

    def fake_build(provider, schema):
        construction_calls.append((provider, schema))
        return StubLLM(result=SimpleNamespace(value="should-not-run"))

    monkeypatch.setattr(
        llm_module, "build_structured_llm_for_provider", fake_build
    )

    with pytest.raises(LLMUnavailable):
        invoke_structured(gemini, "prompt", purpose="root-cause synthesis")

    assert gemini.calls == 1
    assert construction_calls == []
    assert no_sleeping == []
