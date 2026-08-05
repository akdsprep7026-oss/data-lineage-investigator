"""Tests for the Step 9 Langfuse tracing helpers.

The suite-wide conftest clears Langfuse credentials, so these check the
offline / no-op path that production code takes when keys aren't set.
Live dashboard verification is done manually via `python -m app.graph.run_test`.
"""

from __future__ import annotations

from app.graph.tracing import (
    get_langchain_callbacks,
    investigation_trace,
    traced_node,
    tracing_enabled,
)


def test_tracing_is_disabled_in_the_test_suite():
    assert tracing_enabled() is False
    assert get_langchain_callbacks() == []


def test_traced_node_is_a_transparent_no_op_when_tracing_is_off():
    calls: list[dict] = []

    @traced_node("example")
    def example_node(state: dict) -> dict:
        calls.append(state)
        return {"status": "ok"}

    result = example_node({"investigation_id": "abc", "retry_count": 0})

    assert result == {"status": "ok"}
    assert calls == [{"investigation_id": "abc", "retry_count": 0}]


def test_investigation_trace_yields_none_when_tracing_is_off():
    with investigation_trace("inv-1", "something broke") as root:
        assert root is None
