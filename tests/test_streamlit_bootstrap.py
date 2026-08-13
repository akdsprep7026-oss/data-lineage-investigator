"""Lightweight tests for Streamlit Cloud bootstrap / deployment helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.streamlit_support import (
    SANDBOX_DEBUG_CLEAN,
    apply_sandbox_debug_selection,
    cloud_database_url_error,
    ensure_retrieval_index,
    ensure_runtime_assets_once,
    ensure_sandbox_warehouse,
    ensure_streamlit_startup_once,
    is_sandbox_debug_enabled,
    is_streamlit_cloud_runtime,
    reset_runtime_bootstrap_for_tests,
    reset_startup_reaper_for_tests,
    runtime_config_status,
)


def test_is_streamlit_cloud_runtime_respects_explicit_flag(monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("STREAMLIT_CLOUD_DEPLOY", "true")
    assert is_streamlit_cloud_runtime() is True
    monkeypatch.setenv("STREAMLIT_CLOUD_DEPLOY", "0")
    assert is_streamlit_cloud_runtime() is False


def test_cloud_database_url_error_none_when_local(monkeypatch):
    monkeypatch.delenv("STREAMLIT_CLOUD_DEPLOY", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setenv("HOME", "C:\\Users\\dev")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cloud_database_url_error() is None


def test_cloud_database_url_error_when_cloud_missing_dsn(monkeypatch):
    monkeypatch.setenv("STREAMLIT_CLOUD_DEPLOY", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    message = cloud_database_url_error()
    assert message is not None
    assert "DATABASE_URL" in message
    assert "your-" not in message.lower()
    # Never include a fabricated secret value.
    assert "postgresql://" not in message


def test_cloud_database_url_error_none_when_cloud_has_dsn(monkeypatch):
    monkeypatch.setenv("STREAMLIT_CLOUD_DEPLOY", "true")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:secret-password@db.example/app?sslmode=require",
    )
    assert cloud_database_url_error() is None
    status = runtime_config_status()
    blob = " ".join(status.values())
    assert "secret-password" not in blob
    assert status["database"] == "DATABASE_URL"


def test_ensure_sandbox_warehouse_skips_seed_when_ready(monkeypatch):
    monkeypatch.setattr(
        "app.streamlit_support.is_sandbox_warehouse_ready", lambda: True
    )
    with patch("app.sandbox_data.seed.seed") as seed_mock:
        assert ensure_sandbox_warehouse() == "ready"
        seed_mock.assert_not_called()


def test_ensure_sandbox_warehouse_seeds_when_missing(monkeypatch):
    ready_calls = {"n": 0}

    def _ready() -> bool:
        ready_calls["n"] += 1
        return ready_calls["n"] > 1

    monkeypatch.setattr(
        "app.streamlit_support.is_sandbox_warehouse_ready", _ready
    )
    with patch("app.sandbox_data.seed.seed") as seed_mock:
        assert ensure_sandbox_warehouse() == "seeded"
        seed_mock.assert_called_once_with()


def test_ensure_retrieval_index_skips_ingest_when_ready(monkeypatch):
    monkeypatch.setattr("app.streamlit_support.is_retrieval_ready", lambda: True)
    with patch("app.retrieval.ingest.ingest") as ingest_mock:
        assert ensure_retrieval_index() == "ready"
        ingest_mock.assert_not_called()


def test_ensure_retrieval_index_ingests_when_missing(monkeypatch):
    ready_calls = {"n": 0}

    def _ready() -> bool:
        ready_calls["n"] += 1
        return ready_calls["n"] > 1

    monkeypatch.setattr("app.streamlit_support.is_retrieval_ready", _ready)
    with patch("app.retrieval.ingest.ingest", return_value=7) as ingest_mock:
        assert ensure_retrieval_index() == "ingested:7"
        ingest_mock.assert_called_once_with(reset=True)


def test_ensure_runtime_assets_once_runs_only_once(monkeypatch):
    reset_runtime_bootstrap_for_tests()
    warehouse = MagicMock(return_value="ready")
    retrieval = MagicMock(return_value="ready")
    monkeypatch.setattr(
        "app.streamlit_support.ensure_sandbox_warehouse", warehouse
    )
    monkeypatch.setattr(
        "app.streamlit_support.ensure_retrieval_index", retrieval
    )
    first = ensure_runtime_assets_once()
    second = ensure_runtime_assets_once()
    assert first == {"warehouse": "ready", "retrieval": "ready"}
    assert second == first
    assert warehouse.call_count == 1
    assert retrieval.call_count == 1
    reset_runtime_bootstrap_for_tests()


def test_ensure_streamlit_startup_once_orders_reaper_then_assets(monkeypatch):
    reset_startup_reaper_for_tests()
    reset_runtime_bootstrap_for_tests()
    order: list[str] = []

    def _reaper() -> None:
        order.append("reaper")

    def _assets() -> dict[str, str]:
        order.append("assets")
        return {"warehouse": "ready", "retrieval": "ready"}

    monkeypatch.setattr(
        "app.streamlit_support.ensure_startup_reaper_once", _reaper
    )
    monkeypatch.setattr(
        "app.streamlit_support.ensure_runtime_assets_once", _assets
    )
    result = ensure_streamlit_startup_once()
    assert order == ["reaper", "assets"]
    assert result["warehouse"] == "ready"
    reset_startup_reaper_for_tests()
    reset_runtime_bootstrap_for_tests()


def test_sandbox_debug_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_SANDBOX_DEBUG", raising=False)
    assert is_sandbox_debug_enabled() is False
    monkeypatch.setenv("ENABLE_SANDBOX_DEBUG", "false")
    assert is_sandbox_debug_enabled() is False
    monkeypatch.setenv("ENABLE_SANDBOX_DEBUG", "true")
    assert is_sandbox_debug_enabled() is True


def test_apply_sandbox_debug_selection_clean_resets_and_ingests(monkeypatch):
    reset = MagicMock()
    ingest = MagicMock(return_value=7)
    counts = {
        "raw_customers": 40,
        "raw_orders": 200,
        "stg_orders_cleaned": 167,
        "fct_daily_revenue": 54,
    }
    monkeypatch.setattr(
        "app.sandbox_data.incidents.common.reset_to_clean_baseline", reset
    )
    monkeypatch.setattr("app.retrieval.ingest.ingest", ingest)
    monkeypatch.setattr(
        "app.streamlit_support.sandbox_warehouse_row_counts", lambda: counts
    )

    result = apply_sandbox_debug_selection(SANDBOX_DEBUG_CLEAN)

    assert result["ok"] is True
    assert "clean baseline" in result["message"].lower()
    assert result["docs_ingested"] == 7
    assert result["counts"] == counts
    reset.assert_called_once_with()
    ingest.assert_called_once_with(reset=True)


def test_apply_sandbox_debug_selection_applies_incident(monkeypatch):
    apply_fn = MagicMock()
    incident_mod = MagicMock(apply=apply_fn)
    ingest = MagicMock(return_value=7)
    monkeypatch.setattr(
        "app.sandbox_data.incidents.manage.INCIDENTS",
        {"1": incident_mod, "2": MagicMock(), "3": MagicMock(), "4": MagicMock()},
    )
    monkeypatch.setattr("app.retrieval.ingest.ingest", ingest)
    monkeypatch.setattr(
        "app.streamlit_support.sandbox_warehouse_row_counts",
        lambda: {"raw_orders": 205},
    )

    result = apply_sandbox_debug_selection("1")

    assert result["ok"] is True
    assert "incident 1" in result["message"].lower()
    apply_fn.assert_called_once_with()
    ingest.assert_called_once_with(reset=True)


def test_apply_sandbox_debug_selection_rejects_unknown():
    result = apply_sandbox_debug_selection("99")
    assert result["ok"] is False
    assert "unknown" in result["message"].lower()
    assert result["docs_ingested"] == 0
