"""Database engine/session setup shared by app/db/*.

If DATABASE_URL (see .env) is set, that DSN is always used -- this is
what production/staging (e.g. Neon on Render) should do. A failed
connect raises; we do not silently fall back to embedded Postgres when
a production URL is configured.

If DATABASE_URL is unset/empty (typical fresh local checkout), we
transparently fall back to a self-contained embedded Postgres server
via the `pgserver` package, backed by a persistent local data directory
(app/db/.pgdata) so data survives across process restarts. This keeps
`investigations` a real Postgres table (JSONB, native enum, etc.) in
every environment without requiring a manual Postgres install just to
run tests locally.

Unclean shutdowns (laptop reboot, killed uvicorn) can leave a stale
`postmaster.pid` or a half-recovered data directory that makes the next
`pgserver.get_server()` raise a timeout or AssertionError. Startup here
clears dead locks automatically and, as a last resort for this local
dev store only, wipes and reinitializes `.pgdata`.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()

logger = logging.getLogger(__name__)

APP_DB_DIR = Path(__file__).resolve().parent
EMBEDDED_PGDATA_DIR = APP_DB_DIR / ".pgdata"
PROJECT_ROOT = APP_DB_DIR.parents[1]

_embedded_server = None
_engine: Optional[Engine] = None


def _pid_is_alive(pid: int) -> bool:
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return False
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001 - treat unknown as dead for lock cleanup
        return False


def _read_postmaster_pid(pgdata: Path) -> Optional[int]:
    pid_file = pgdata / "postmaster.pid"
    if not pid_file.exists():
        return None
    try:
        first_line = pid_file.read_text(encoding="utf-8").splitlines()[0].strip()
        return int(first_line)
    except (OSError, IndexError, ValueError):
        return None


def _clear_stale_postmaster_lock(pgdata: Path) -> bool:
    """Remove `postmaster.pid` when it doesn't point at a live process.

    Returns True if a stale (or unreadable) lock file was removed.
    """
    pid_file = pgdata / "postmaster.pid"
    if not pid_file.exists():
        return False

    pid = _read_postmaster_pid(pgdata)
    if pid is not None and _pid_is_alive(pid):
        return False

    logger.warning(
        "Removing stale embedded-Postgres lock at %s (postmaster pid=%s is not running). "
        "This usually follows an unclean shutdown (reboot / killed process).",
        pid_file,
        pid,
    )
    try:
        pid_file.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove %s: %s", pid_file, exc)
        return False

    handle_pids = pgdata / ".handle_pids.json"
    try:
        handle_pids.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _kill_postgres_for_pgdata(pgdata: Path) -> None:
    """Terminate leftover postgres processes still bound to this pgdata."""
    try:
        import psutil
    except ImportError:
        return

    pgdata_resolved = str(pgdata.resolve())
    victims = []
    for proc in psutil.process_iter(attrs=["name", "cmdline", "pid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "postgres" not in name:
                continue
            cmdline = proc.info.get("cmdline") or []
            if any(pgdata_resolved in str(arg) for arg in cmdline):
                victims.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    for proc in victims:
        try:
            logger.warning(
                "Terminating leftover postgres process pid=%s for %s",
                proc.pid,
                pgdata_resolved,
            )
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    _, still_alive = psutil.wait_procs(victims, timeout=3)
    for proc in still_alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Give Windows a moment to release file handles on .pgdata/log etc.
    if victims:
        time.sleep(1.0)


def _drop_pgserver_cached_instance(pgdata: Path) -> None:
    """Forget pgserver's in-process handle so the next get_server rebuilds."""
    try:
        from pgserver.postgres_server import PostgresServer

        PostgresServer._instances.pop(pgdata.resolve(), None)
        PostgresServer._instances.pop(pgdata, None)
    except Exception:  # noqa: BLE001
        pass


def _reinitialize_pgdata(pgdata: Path) -> None:
    """Wipe the embedded data directory. Local dev/test data only."""
    logger.warning(
        "Reinitializing embedded Postgres data directory at %s "
        "(local dev/test store; investigations table will be recreated via Alembic).",
        pgdata,
    )
    _kill_postgres_for_pgdata(pgdata)
    _drop_pgserver_cached_instance(pgdata)
    if pgdata.exists():
        shutil.rmtree(pgdata, ignore_errors=False)
    pgdata.mkdir(parents=True, exist_ok=True)


def _run_alembic_upgrade() -> None:
    """Apply migrations after a wipe so the investigations table exists again."""
    from alembic import command
    from alembic.config import Config

    ini_path = PROJECT_ROOT / "alembic.ini"
    logger.info("Running alembic upgrade head against the reinitialized embedded DB...")
    config = Config(str(ini_path))
    # env.py resolves the URL via get_database_url(), which will reuse the
    # server we just started (_embedded_server is already set).
    command.upgrade(config, "head")


def _start_embedded_postgres() -> str:
    """Starts (or reuses) a local embedded Postgres server and returns
    its connection URI. The server process is torn down when this
    process exits (cleanup_mode='stop'); the underlying data directory
    persists on disk across runs.

    Recovery strategy on failure:
      1. Clear a stale postmaster.pid (dead PID) and retry.
      2. Kill leftover postgres processes for this pgdata, clear locks, retry.
      3. Wipe `.pgdata` and reinitialize (dev/test data only), then migrate.
    """
    global _embedded_server
    import pgserver

    EMBEDDED_PGDATA_DIR.mkdir(parents=True, exist_ok=True)
    if _embedded_server is not None:
        return _embedded_server.get_uri()

    # Cheap first pass: drop a lock left behind by a reboot / kill -9.
    _clear_stale_postmaster_lock(EMBEDDED_PGDATA_DIR)

    attempts = (
        "start",
        "retry_after_process_cleanup",
        "reinitialize_pgdata",
    )
    last_error: Optional[BaseException] = None

    for attempt_name in attempts:
        try:
            if attempt_name == "retry_after_process_cleanup":
                logger.warning(
                    "Embedded Postgres failed to start; cleaning leftover processes "
                    "and locks under %s, then retrying.",
                    EMBEDDED_PGDATA_DIR,
                )
                _kill_postgres_for_pgdata(EMBEDDED_PGDATA_DIR)
                # Force-remove the pid file even if a zombie PID looked "alive".
                (EMBEDDED_PGDATA_DIR / "postmaster.pid").unlink(missing_ok=True)
                (EMBEDDED_PGDATA_DIR / ".handle_pids.json").unlink(missing_ok=True)
                _drop_pgserver_cached_instance(EMBEDDED_PGDATA_DIR)
            elif attempt_name == "reinitialize_pgdata":
                _reinitialize_pgdata(EMBEDDED_PGDATA_DIR)

            _embedded_server = pgserver.get_server(
                EMBEDDED_PGDATA_DIR, cleanup_mode="stop"
            )
            uri = _embedded_server.get_uri()

            if attempt_name == "reinitialize_pgdata":
                _run_alembic_upgrade()

            if attempt_name != "start":
                logger.info(
                    "Embedded Postgres recovered via %s; uri host/port from postmaster.pid.",
                    attempt_name,
                )
            return uri
        except Exception as exc:  # noqa: BLE001 - try next recovery step
            last_error = exc
            _embedded_server = None
            _drop_pgserver_cached_instance(EMBEDDED_PGDATA_DIR)
            logger.exception(
                "Embedded Postgres start attempt %r failed: %s: %s",
                attempt_name,
                type(exc).__name__,
                exc,
            )

    log_path = EMBEDDED_PGDATA_DIR / "log"
    log_hint = (
        f" Inspect the Postgres log at {log_path}."
        if log_path.exists()
        else ""
    )
    raise RuntimeError(
        f"Could not start embedded Postgres at {EMBEDDED_PGDATA_DIR}. "
        f"Last error: {type(last_error).__name__}: {last_error}.{log_hint} "
        f"As a manual last resort, stop every python/postgres process using that "
        f"directory, delete {EMBEDDED_PGDATA_DIR}, and restart the API — or set "
        f"DATABASE_URL to a real Postgres instance."
    ) from last_error


def get_database_url() -> str:
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if database_url:
        # Production/staging: honor the configured DSN. Do not fall back
        # to embedded Postgres if Neon (or any remote) is briefly unreachable.
        try:
            probe_engine = create_engine(database_url)
            with probe_engine.connect():
                pass
        except Exception as exc:
            raise RuntimeError(
                "DATABASE_URL is set but the database is unreachable. "
                "Fix the DSN (Neon typically needs sslmode=require) or "
                "unset DATABASE_URL to use the local embedded Postgres fallback."
            ) from exc
        return database_url
    return _start_embedded_postgres()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        # pool_pre_ping: before handing out a pooled connection, emit a
        # cheap SELECT 1 (or dialect equivalent) and replace the
        # connection if it is dead. Needed for Neon/Render where idle
        # SSL sessions are closed server-side while the process-global
        # engine still holds them in the default QueuePool.
        _engine = create_engine(get_database_url(), pool_pre_ping=True)
    return _engine


def get_session() -> Session:
    session_factory = sessionmaker(bind=get_engine())
    return session_factory()
