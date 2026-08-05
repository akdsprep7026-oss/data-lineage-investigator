"""Database engine/session setup shared by app/db/*.

If DATABASE_URL (see .env) points at a real, reachable Postgres
instance, that's what gets used -- this is what production/staging
should always do. If it's unset or unreachable (e.g. a fresh local dev
checkout with no Postgres server installed), we transparently fall back
to a self-contained embedded Postgres server via the `pgserver` package,
backed by a persistent local data directory (app/db/.pgdata) so data
survives across process restarts. This keeps `investigations` a real
Postgres table (JSONB, native enum, etc.) in every environment without
requiring a manual Postgres install just to run tests locally.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()

APP_DB_DIR = Path(__file__).resolve().parent
EMBEDDED_PGDATA_DIR = APP_DB_DIR / ".pgdata"

_embedded_server = None
_engine: Optional[Engine] = None


def _start_embedded_postgres() -> str:
    """Starts (or reuses) a local embedded Postgres server and returns
    its connection URI. The server process is torn down when this
    process exits (cleanup_mode='stop'); the underlying data directory
    persists on disk across runs."""
    global _embedded_server
    import pgserver

    EMBEDDED_PGDATA_DIR.mkdir(parents=True, exist_ok=True)
    if _embedded_server is None:
        _embedded_server = pgserver.get_server(
            EMBEDDED_PGDATA_DIR, cleanup_mode="stop"
        )
    return _embedded_server.get_uri()


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            probe_engine = create_engine(database_url)
            with probe_engine.connect():
                pass
            return database_url
        except Exception:
            pass  # fall through to the embedded Postgres server below
    return _start_embedded_postgres()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_database_url())
    return _engine


def get_session() -> Session:
    session_factory = sessionmaker(bind=get_engine())
    return session_factory()
