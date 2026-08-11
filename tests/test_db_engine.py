"""Tests for the Investigations PostgreSQL engine configuration.

Focused on pool_pre_ping so Neon/Render idle SSL disconnects are
detected on checkout rather than surfacing as OperationalError mid-
request. Does not cover the Sandbox SQLite engine.
"""

from __future__ import annotations

import app.db.base as db_base
from app.db.base import get_engine
from app.sandbox_data.models import get_engine as get_sandbox_engine


def test_investigations_engine_enables_pool_pre_ping():
    """The long-lived Investigations engine must pre-ping pooled connections."""
    previous = db_base._engine
    db_base._engine = None
    try:
        engine = get_engine()
        assert engine.pool._pre_ping is True
    finally:
        # Restore any prior singleton so later tests keep a shared engine.
        if previous is not None:
            if db_base._engine is not None and db_base._engine is not previous:
                db_base._engine.dispose()
            db_base._engine = previous


def test_sandbox_sqlite_engine_is_unaffected_by_investigations_pool_settings():
    """Sandbox warehouse uses its own create_engine; it must not inherit
    Investigations pool_pre_ping configuration."""
    sandbox_engine = get_sandbox_engine()
    assert getattr(sandbox_engine.pool, "_pre_ping", False) is False
