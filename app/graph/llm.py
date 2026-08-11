"""Shared LLM access for the specialist nodes (app/graph/sql_review.py
and app/graph/root_cause.py).

Provider selection is driven by LLM_PROVIDER in .env:

    LLM_PROVIDER=gemini   (default) Gemini via GOOGLE_API_KEY
    LLM_PROVIDER=groq               Groq via GROQ_API_KEY
    (neither key usable)            the offline heuristics take over

When LLM_PROVIDER=gemini and both keys are configured, a call that
exhausts Gemini (auth failure, hard quota exhaustion, or retries
spent) automatically attempts Groq once before raising LLMUnavailable.
Callers catch that and fall back to their deterministic heuristic.

When LLM_PROVIDER=groq, Groq is the only cloud provider tried -- Gemini
is never forced into the chain.

Retrieval is unaffected: Groq has no embeddings endpoint, so
app/retrieval/embeddings.py keeps its own Gemini-or-local-ONNX choice.

Calls go through invoke_structured(), which retries *transient*
failures with exponential backoff per provider, then optionally fails
over to a secondary provider, then raises LLMUnavailable. Hard daily
quota exhaustion and auth failures skip pointless retries. Schema /
malformed-output errors still propagate unchanged.

When a Langfuse investigation trace is open (see app/graph/tracing.py),
each invoke also receives Langfuse's LangChain CallbackHandler so token
usage -- and cost, when Langfuse has a price for the model -- is
attached to the generation out of the box for both Gemini and Groq.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

from dotenv import load_dotenv

from app.graph.tracing import get_langchain_callbacks

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "gemini"
PROVIDERS = ("gemini", "groq")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
# Groq deprecates models on fairly short notice, so this is overridable
# without a code change. See https://console.groq.com/docs/models.
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

API_KEY_ENV_VARS = {"gemini": "GOOGLE_API_KEY", "groq": "GROQ_API_KEY"}

# .env.example ships placeholders like "your-groq-api-key-here", and a
# copied-but-unedited .env is a very easy mistake to make. Treating them
# as unset turns a confusing wall of 401s into a clean fall-through to
# the next usable provider.
PLACEHOLDER_PREFIX = "your-"

MAX_ATTEMPTS = 3
INITIAL_BACKOFF_SECONDS = 2.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 30.0

# Failures worth waiting out: throttling, transient server errors and
# network blips. Note: hard quota exhaustion is detected separately and
# is NOT retried even though it often also contains "429".
RETRYABLE_MARKERS = (
    "429",
    "resource_exhausted",
    "rate limit",
    "rate_limit",
    "too many requests",
    "500",
    "502",
    "503",
    "504",
    "internal server error",
    "service unavailable",
    "unavailable",
    "overloaded",
    "timeout",
    "timed out",
    "connection reset",
    "connection error",
    "temporarily unavailable",
)

# Hard/daily quota signals. Sleeping will not clear these; fail over
# (or raise LLMUnavailable) immediately. Conservative: require an
# explicit quota/free-tier cue, not bare "429" / "resource_exhausted".
HARD_QUOTA_MARKERS = (
    "quota exceeded",
    "quota_exceeded",
    "exceeded your current quota",
    "free_tier",
    "free tier",
    "generate_content_free_tier",
    "daily limit",
    "daily quota",
    "insufficient_quota",
)

# Failures that won't improve on retry but that the caller can still
# route around by using its heuristic: a bad/missing key, a revoked
# permission, a model id that no longer exists.
DEGRADABLE_MARKERS = (
    "401",
    "403",
    "404",
    "unauthenticated",
    "unauthorized",
    "permission_denied",
    "permission denied",
    "invalid api key",
    "invalid_api_key",
    "api key not valid",
    "model_not_found",
    "does not exist",
    "decommissioned",
)


class LLMUnavailable(RuntimeError):
    """The model couldn't be reached for this call. Callers should fall
    back to their offline heuristic rather than failing the run."""


def _configured_key(provider: str) -> Optional[str]:
    value = (os.getenv(API_KEY_ENV_VARS[provider]) or "").strip()
    if not value or value.lower().startswith(PLACEHOLDER_PREFIX):
        return None
    return value


def requested_provider() -> str:
    return (os.getenv("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()


def resolve_provider() -> Optional[str]:
    """The provider actually usable right now, or None if the offline
    heuristics should be used instead.

    The provider named by LLM_PROVIDER wins if its key is configured.
    Otherwise the other one is used if *its* key is configured, so an
    unset or mistyped LLM_PROVIDER degrades to a working setup rather
    than to no LLM at all.
    """
    requested = requested_provider()
    if requested not in PROVIDERS:
        logger.warning(
            "Unknown LLM_PROVIDER=%r; expected one of %s. Falling back.",
            requested,
            ", ".join(PROVIDERS),
        )
        requested = DEFAULT_PROVIDER

    for provider in (requested, *(p for p in PROVIDERS if p != requested)):
        if _configured_key(provider):
            if provider != requested:
                logger.warning(
                    "LLM_PROVIDER=%s but %s is not configured; using %s instead.",
                    requested,
                    API_KEY_ENV_VARS[requested],
                    provider,
                )
            return provider
    return None


def llm_enabled() -> bool:
    return resolve_provider() is not None


def _model_for_provider(provider: str) -> str:
    return GROQ_MODEL if provider == "groq" else GEMINI_MODEL


def _provider_label(provider: str) -> str:
    return f"{provider}:{_model_for_provider(provider)}"


def active_model_label() -> str:
    """Human-readable description of what will actually answer, for
    logging and for the smoke-test scripts."""
    provider = resolve_provider()
    if provider is None:
        return "offline heuristics (no LLM provider configured)"
    return _provider_label(provider)


def _secondary_provider(primary: str) -> Optional[str]:
    """Cloud provider to try after `primary` fails, or None.

    Auto-failover only runs when the user requested Gemini and Groq is
    also configured. Explicit LLM_PROVIDER=groq never pulls Gemini in.
    """
    requested = requested_provider()
    if requested not in PROVIDERS:
        requested = DEFAULT_PROVIDER
    if requested != "gemini" or primary != "gemini":
        return None
    if _configured_key("groq"):
        return "groq"
    return None


def _build_chat_model(provider: str) -> Any:
    if provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(model=GROQ_MODEL, temperature=0)
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model=GEMINI_MODEL, temperature=0)


def build_structured_llm_for_provider(provider: str, schema: type) -> Any:
    """Bind `schema` to a specific provider's chat model."""
    return _build_chat_model(provider).with_structured_output(schema)


def build_structured_llm(schema: type) -> Optional[Any]:
    """Returns a chat model bound to `schema` for structured output, or
    None if no provider is configured."""
    provider = resolve_provider()
    if provider is None:
        return None
    return build_structured_llm_for_provider(provider, schema)


def _matches(exc: BaseException, markers: tuple[str, ...]) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in markers)


def _is_hard_quota_exhaustion(exc: BaseException) -> bool:
    """True when the error indicates a quota/daily free-tier limit that
    will not clear with short backoff. Bare 429 / RESOURCE_EXHAUSTED
    without a quota cue is treated as possibly transient instead."""
    return _matches(exc, HARD_QUOTA_MARKERS)


def _server_requested_delay(exc: BaseException) -> Optional[float]:
    """Honours a server-supplied backoff hint when there is one, e.g.
    Gemini's "Please retry in 30.79s" or a retryDelay of '30s'."""
    text = str(exc)
    match = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", text, re.IGNORECASE)
    if match:
        return float(match.group(1))
    match = re.search(r"retrydelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", text, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def _invoke_provider_with_retry(
    llm: Any,
    prompt: str,
    *,
    purpose: str,
    provider_label: str,
) -> Any:
    """Provider-local invoke with exponential backoff.

    Raises LLMUnavailable for auth/quota/retry exhaustion. Propagates
    unrecognized errors (e.g. schema mismatches) unchanged.
    """
    delay = INITIAL_BACKOFF_SECONDS
    last_error: Optional[BaseException] = None
    callbacks = get_langchain_callbacks()
    invoke_config = {"callbacks": callbacks} if callbacks else None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if invoke_config is not None:
                return llm.invoke(prompt, config=invoke_config)
            return llm.invoke(prompt)
        except Exception as exc:
            last_error = exc

            if _is_hard_quota_exhaustion(exc):
                raise LLMUnavailable(
                    f"{purpose}: {provider_label} quota exhausted "
                    f"({type(exc).__name__}). Not retrying."
                ) from exc

            if _matches(exc, DEGRADABLE_MARKERS) and not _matches(
                exc, RETRYABLE_MARKERS
            ):
                raise LLMUnavailable(
                    f"{purpose}: {provider_label} rejected the request "
                    f"({type(exc).__name__}). Not retrying."
                ) from exc

            if not _matches(exc, RETRYABLE_MARKERS):
                raise

            if attempt == MAX_ATTEMPTS:
                break

            wait = min(
                max(delay, _server_requested_delay(exc) or 0.0), MAX_BACKOFF_SECONDS
            )
            logger.warning(
                "%s: attempt %d/%d failed (%s). Retrying in %.1fs.",
                purpose,
                attempt,
                MAX_ATTEMPTS,
                type(exc).__name__,
                wait,
            )
            time.sleep(wait)
            delay *= BACKOFF_MULTIPLIER

    raise LLMUnavailable(
        f"{purpose}: {provider_label} still failing after {MAX_ATTEMPTS} "
        f"attempts ({type(last_error).__name__ if last_error else 'unknown'})."
    ) from last_error


def invoke_structured(
    llm: Any,
    prompt: str,
    *,
    purpose: str,
    schema: Optional[type] = None,
    secondary_llm: Any = None,
) -> Any:
    """Invokes `llm` with provider-local retries, then optional failover.

    Pass `schema` (and optionally a pre-built `secondary_llm` for tests)
    so that when the primary provider raises LLMUnavailable and the
    user requested Gemini with Groq also configured, Groq is tried
    before LLMUnavailable propagates to the caller's heuristic.

    Schema / malformed-output errors are never converted into failover
    or heuristics -- they propagate unchanged.
    """
    primary = resolve_provider()
    primary_label = (
        _provider_label(primary) if primary is not None else active_model_label()
    )

    try:
        return _invoke_provider_with_retry(
            llm, prompt, purpose=purpose, provider_label=primary_label
        )
    except LLMUnavailable as primary_exc:
        secondary = _secondary_provider(primary) if primary is not None else None
        if secondary is None and secondary_llm is None:
            raise

        secondary_label = (
            _provider_label(secondary) if secondary is not None else "secondary"
        )
        logger.warning(
            "%s: %s unavailable (%s); attempting %s fallback.",
            purpose,
            primary_label,
            type(primary_exc).__name__,
            secondary_label,
        )

        failover_llm = secondary_llm
        if failover_llm is None:
            if schema is None or secondary is None:
                raise
            failover_llm = build_structured_llm_for_provider(secondary, schema)

        try:
            result = _invoke_provider_with_retry(
                failover_llm,
                prompt,
                purpose=purpose,
                provider_label=secondary_label,
            )
        except LLMUnavailable as secondary_exc:
            logger.warning(
                "%s: %s unavailable (%s); raising LLMUnavailable for "
                "heuristic fallback.",
                purpose,
                secondary_label,
                type(secondary_exc).__name__,
            )
            raise LLMUnavailable(
                f"{purpose}: {primary_label} and {secondary_label} unavailable "
                f"({type(primary_exc).__name__}, {type(secondary_exc).__name__})."
            ) from secondary_exc

        logger.info("%s: %s fallback succeeded.", purpose, secondary_label)
        return result
