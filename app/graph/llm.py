"""Shared LLM access for the specialist nodes (app/graph/sql_review.py
and app/graph/root_cause.py).

Extends the existing two-way pattern -- real model if configured, local
heuristic otherwise -- into a three-way one. The provider is chosen by
LLM_PROVIDER in .env:

    LLM_PROVIDER=gemini   (default) Gemini via GOOGLE_API_KEY
    LLM_PROVIDER=groq               Groq via GROQ_API_KEY
    (neither key usable)            the offline heuristics take over

The point of the Groq path is to keep Gemini's small daily free-tier
quota in reserve. Dev and test iterations can run against Groq, leaving
Gemini for final evaluation runs. Retrieval is unaffected either way:
Groq has no embeddings endpoint, so app/retrieval/embeddings.py keeps
its own Gemini-or-local-ONNX choice.

Calls go through invoke_structured(), which retries transient failures
with exponential backoff and then gives up by raising LLMUnavailable.
Callers are expected to catch that and fall back to their heuristic for
that one call, so a rate limit costs an investigation some accuracy on
a single step instead of aborting the whole run. Note that backoff only
helps with per-minute throttling; a *daily* quota won't clear no matter
how long we wait, which is precisely why the give-up path has to exist.

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
# network blips.
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


def active_model_label() -> str:
    """Human-readable description of what will actually answer, for
    logging and for the smoke-test scripts."""
    provider = resolve_provider()
    if provider is None:
        return "offline heuristics (no LLM provider configured)"
    return f"{provider}:{GROQ_MODEL if provider == 'groq' else GEMINI_MODEL}"


def build_structured_llm(schema: type) -> Optional[Any]:
    """Returns a chat model bound to `schema` for structured output, or
    None if no provider is configured."""
    provider = resolve_provider()
    if provider is None:
        return None

    if provider == "groq":
        from langchain_groq import ChatGroq

        model = ChatGroq(model=GROQ_MODEL, temperature=0)
    else:
        from langchain_google_genai import ChatGoogleGenerativeAI

        model = ChatGoogleGenerativeAI(model=GEMINI_MODEL, temperature=0)

    return model.with_structured_output(schema)


def _matches(exc: BaseException, markers: tuple[str, ...]) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in markers)


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


def invoke_structured(llm: Any, prompt: str, *, purpose: str) -> Any:
    """Invokes `llm`, retrying transient failures with exponential
    backoff up to MAX_ATTEMPTS times.

    Raises LLMUnavailable when the call can't be completed but the
    caller could reasonably carry on without it (retries exhausted, or a
    key/model problem that retrying won't fix). Anything else -- a bad
    prompt, a schema mismatch, a bug in our own code -- propagates
    unchanged, so real defects stay loud instead of being quietly
    swallowed as "the model was busy".
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

            if _matches(exc, DEGRADABLE_MARKERS) and not _matches(exc, RETRYABLE_MARKERS):
                raise LLMUnavailable(
                    f"{purpose}: {active_model_label()} rejected the request "
                    f"({exc}). Not retrying."
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
        f"{purpose}: {active_model_label()} still failing after {MAX_ATTEMPTS} "
        f"attempts ({last_error})."
    ) from last_error
