"""Shared pytest configuration.

Forces every test onto the offline heuristic fallbacks by clearing both
provider API keys (see app/graph/llm.py). Individual tests that need a
provider configured set the relevant key themselves via monkeypatch,
which overrides this.

This is a suite-wide fixture rather than a per-test one deliberately: a
test that accidentally reaches a live model is slow, costs quota, and --
worst of all -- stops being deterministic. Clearing the keys in one
place means adding a new provider can't silently reintroduce that.
"""

from __future__ import annotations

import pytest

from app.graph.llm import API_KEY_ENV_VARS


@pytest.fixture(autouse=True)
def force_offline_llm(monkeypatch):
    for env_var in API_KEY_ENV_VARS.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
