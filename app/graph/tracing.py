"""Langfuse observability for the investigation LangGraph.

Step 9: every node span, every LLM call, and every retry pass of the
loop lands in one Langfuse trace per investigation, keyed by
`investigation_id` as the session id so the dashboard groups the whole
run as one readable trail.

Wiring is opt-in on credentials: when LANGFUSE_PUBLIC_KEY /
LANGFUSE_SECRET_KEY are missing or still hold the .env.example
placeholders, every helper here is a no-op so tests and fresh checkouts
stay offline. When the keys are set, `run_investigation` opens a root
span, each node is wrapped so its inputs/outputs/duration are recorded,
and LLM calls (Gemini or Groq via LangChain) go through Langfuse's
CallbackHandler so token usage -- and cost, when Langfuse knows the
model -- are attached out of the box.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Iterator, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

PLACEHOLDER_PREFIX = "your-"

# LangChain callbacks active for the current investigation run. Read by
# app/graph/llm.py so Gemini/Groq invokes nest under the open node span
# and report token usage through Langfuse's CallbackHandler.
_langchain_callbacks: ContextVar[list[Any]] = ContextVar(
    "langfuse_langchain_callbacks", default=[]
)

# Populated by the most recent investigation_trace() so smoke tests can
# print a dashboard link without threading the URL through the graph.
_last_trace_url: Optional[str] = None
_last_trace_id: Optional[str] = None


def last_trace_url() -> Optional[str]:
    return _last_trace_url


def last_trace_id() -> Optional[str]:
    return _last_trace_id


def _configured(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    if not text or text.lower().startswith(PLACEHOLDER_PREFIX):
        return None
    return text


def tracing_enabled() -> bool:
    """True when Langfuse keys are configured and tracing hasn't been
    explicitly disabled via LANGFUSE_TRACING=false."""
    flag = (os.getenv("LANGFUSE_TRACING") or "true").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    return bool(
        _configured(os.getenv("LANGFUSE_PUBLIC_KEY"))
        and _configured(os.getenv("LANGFUSE_SECRET_KEY"))
    )


def get_langchain_callbacks() -> list[Any]:
    """Callbacks to pass into LangChain `invoke(..., config=...)` calls.
    Empty when tracing is off or no investigation is in flight."""
    return list(_langchain_callbacks.get())


def _json_safe(value: Any, *, max_str: int = 800, max_list: int = 25) -> Any:
    """Shrinks state dumps so a node span stays readable in the UI."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_str:
            return value
        return f"{value[:max_str]}...({len(value)} chars)"
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, max_str=max_str, max_list=max_list)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = list(value)
        rendered = [
            _json_safe(item, max_str=max_str, max_list=max_list)
            for item in items[:max_list]
        ]
        if len(items) > max_list:
            rendered.append(f"...({len(items) - max_list} more)")
        return rendered
    return str(value)


def _node_input_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Compact view of InvestigationState for a node span's input."""
    evidence = state.get("evidence") or []
    hypotheses = state.get("hypotheses") or []
    return _json_safe(
        {
            "investigation_id": state.get("investigation_id"),
            "issue_description": state.get("issue_description"),
            "status": state.get("status"),
            "retry_count": state.get("retry_count", 0),
            "agents_to_run": state.get("agents_to_run"),
            "agents_completed": state.get("agents_completed"),
            "follow_up_query": state.get("follow_up_query"),
            "evidence_count": len(evidence),
            "hypotheses_count": len(hypotheses),
            "relevant_tables": state.get("relevant_tables"),
            "relevant_sql_models": [
                {
                    "file_path": model.get("file_path"),
                    "table_name": model.get("table_name"),
                }
                for model in (state.get("relevant_sql_models") or [])
            ],
            "top_hypothesis": state.get("top_hypothesis"),
            "validation": state.get("validation"),
        }
    )


@contextmanager
def investigation_trace(
    investigation_id: str,
    issue_description: str,
) -> Iterator[Optional[Any]]:
    """Opens the root Langfuse span for one investigation run.

    `investigation_id` is both the session id (so the Langfuse Sessions
    view groups every retry pass together) and a trace metadata field.
    Yields the root span, or None when tracing is disabled.
    """
    global _last_trace_url, _last_trace_id

    if not tracing_enabled():
        _last_trace_url = None
        _last_trace_id = None
        yield None
        return

    from langfuse import Langfuse, get_client, propagate_attributes
    from langfuse.langchain import CallbackHandler

    langfuse = get_client()
    # Seed the OTel trace id from the investigation id so consecutive
    # runs never collapse into one Langfuse trace (a known failure mode
    # when the exporter reuses ambient context across process-local
    # clients). Same investigation_id -> same trace; a new row -> a new
    # trace.
    trace_id = Langfuse.create_trace_id(seed=investigation_id)
    handler = CallbackHandler()
    callbacks_token = _langchain_callbacks.set([handler])
    _last_trace_url = None
    _last_trace_id = None

    try:
        with langfuse.start_as_current_observation(
            as_type="span",
            name="investigation",
            trace_context={"trace_id": trace_id},
            input=_json_safe(
                {
                    "investigation_id": investigation_id,
                    "issue_description": issue_description,
                }
            ),
            metadata={"investigation_id": investigation_id},
        ) as root:
            with propagate_attributes(
                session_id=investigation_id,
                trace_name=f"investigation:{investigation_id}",
                tags=["data-lineage-investigator", "investigation"],
                metadata={"investigation_id": investigation_id},
            ):
                # Optional: LANGFUSE_PUBLIC_TRACES=true makes the run
                # shareable without a project login (useful for verifying
                # the dashboard from a fresh browser session). Set before
                # work starts so the attribute is on the exported root span.
                if (os.getenv("LANGFUSE_PUBLIC_TRACES") or "").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }:
                    try:
                        root.set_trace_as_public()
                    except Exception:  # noqa: BLE001
                        logger.exception("Failed to mark Langfuse trace public")
                yield root
                try:
                    _last_trace_id = langfuse.get_current_trace_id() or trace_id
                    _last_trace_url = langfuse.get_trace_url()
                    if not _last_trace_url and _last_trace_id:
                        base = (
                            os.getenv("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
                        ).rstrip("/")
                        _last_trace_url = f"{base}/trace/{_last_trace_id}"
                except Exception:  # noqa: BLE001 - never fail the run for URL lookup
                    logger.exception("Failed to resolve Langfuse trace URL")
                if _last_trace_url:
                    logger.info(
                        "Langfuse trace for investigation %s: %s",
                        investigation_id,
                        _last_trace_url,
                    )
    finally:
        _langchain_callbacks.reset(callbacks_token)
        try:
            langfuse.flush()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to flush Langfuse events")


def traced_node(node_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: log a node's inputs, outputs, and duration as a
    Langfuse span nested under the open investigation trace.

    When tracing is off the wrapped function is called unchanged, so
    unit tests that invoke nodes directly stay offline and fast.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def wrapper(state: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            if not tracing_enabled():
                return fn(state, *args, **kwargs)

            from langfuse import get_client

            langfuse = get_client()
            started = time.perf_counter()
            with langfuse.start_as_current_observation(
                as_type="span",
                name=node_name,
                input=_node_input_summary(state),
                metadata={
                    "node": node_name,
                    "investigation_id": state.get("investigation_id"),
                    "retry_count": state.get("retry_count", 0),
                },
            ) as span:
                try:
                    result = fn(state, *args, **kwargs)
                except Exception as exc:
                    span.update(
                        level="ERROR",
                        status_message=str(exc),
                        metadata={
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000, 2
                            ),
                        },
                    )
                    raise

                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                span.update(
                    output=_json_safe(result if result is not None else {}),
                    metadata={
                        "node": node_name,
                        "investigation_id": (
                            (result or {}).get("investigation_id")
                            if isinstance(result, dict)
                            else None
                        )
                        or state.get("investigation_id"),
                        "retry_count": state.get("retry_count", 0),
                        "duration_ms": duration_ms,
                        "empty_update": result == {} or result is None,
                    },
                )
                return result

        return wrapper

    return decorator
