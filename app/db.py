"""SQLite engine, session management, and the migration runner.

Migration strategy
------------------
Single-user desktop app, one SQLite file. Alembic's revision graph would cost
more than it returns, so instead there is an ordered list of idempotent
migration steps and a ``schema_version`` row. On startup every step above the
stored version runs and the version is bumped.

Adding a migration: append a ``(version, description, callable)`` tuple to
``MIGRATIONS`` and bump ``SCHEMA_VERSION``. Never edit or reorder an existing
entry.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings, get_settings

# Importing models registers the tables on SQLModel.metadata. Without it
# create_all() silently creates nothing and the first query fails with "no such
# table". Relying on another module having imported it first is an import-order
# trap, so do it here explicitly.
from app import models  # noqa: F401  isort:skip

logger = logging.getLogger(__name__)

_engine: Engine | None = None

SCHEMA_VERSION = 1

FTS_TABLE = "chunk_search"


def _configure_sqlite(dbapi_connection: sqlite3.Connection, _record: object) -> None:
    """Pragmas applied to every new connection.

    WAL matters here: the indexer writes while the UI reads, and the default
    rollback journal would serialize them into lock errors.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.close()


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    settings = settings or get_settings()
    settings.ensure_dirs()

    _engine = create_engine(
        f"sqlite:///{settings.db_path}",
        echo=False,
        # The worker thread and the request threads share the engine, so
        # SQLite's per-connection thread check has to be relaxed. WAL and
        # busy_timeout above make this safe at our write volume.
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def reset_engine() -> None:
    """Dispose the engine. Used by tests between temp databases."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Transactional session. Commits on success, rolls back on any exception."""
    session = Session(get_engine(settings))
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


# ---------------------------------------------------------------------------
# migrations
# ---------------------------------------------------------------------------


def _migration_001_chunk_search(session: Session) -> None:
    """FTS5 index over chunk text.

    Keyword search complements vector search rather than replacing it: exact
    identifiers ("ALFRED_TAILSCALE_HOSTNAME", a filename, an invoice number)
    are what embeddings are worst at and FTS is best at.
    """
    session.exec(  # type: ignore[call-overload]
        text(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
                content,
                path,
                tokenize = 'unicode61 remove_diacritics 2'
            )
            """
        )
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_messages_conversation_created "
        "ON messages (conversation_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_pending_status_created "
        "ON pending_actions (status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_jobs_status_created ON jobs (status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_chunks_file_ordinal ON chunks (file_id, ordinal)",
    ):
        session.exec(text(statement))  # type: ignore[call-overload]


MIGRATIONS: list[tuple[int, str, Callable[[Session], None]]] = [
    (1, "chunk full-text search and helper indexes", _migration_001_chunk_search),
]


def _current_version(session: Session) -> int:
    row = session.exec(  # type: ignore[call-overload]
        text("SELECT version FROM schema_version WHERE id = 1")
    ).first()
    return int(row[0]) if row else 0


def run_migrations(settings: Settings | None = None) -> int:
    """Apply every pending migration. Returns the resulting schema version."""
    engine = get_engine(settings)
    SQLModel.metadata.create_all(engine)

    with session_scope(settings) as session:
        session.exec(  # type: ignore[call-overload]
            text("INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 0)")
        )
        version = _current_version(session)
        applied = 0
        for target, description, migrate in MIGRATIONS:
            if target <= version:
                continue
            logger.info("Applying migration %d: %s", target, description)
            migrate(session)
            session.exec(  # type: ignore[call-overload]
                text(
                    "UPDATE schema_version "
                    "SET version = :v, applied_at = CURRENT_TIMESTAMP WHERE id = 1"
                ).bindparams(v=target)
            )
            version = target
            applied += 1
        if applied:
            logger.info("Applied %d migration(s). Schema now at version %d.", applied, version)
        return version


def init_db(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    settings.ensure_dirs()
    version = run_migrations(settings)
    if version != SCHEMA_VERSION:
        logger.warning(
            "Schema version %d does not match expected %d. "
            "The database may have been created by a newer build.",
            version,
            SCHEMA_VERSION,
        )
