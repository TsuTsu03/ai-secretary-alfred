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

SCHEMA_VERSION = 2

FTS_TABLE = "chunk_search"
VEC_TABLE = "chunk_vectors"


_vec_available: bool | None = None


def _load_sqlite_vec(dbapi_connection: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension onto a fresh connection.

    Vectors live in the same database as their metadata so that indexing a file
    and storing its embeddings is one transaction. A separate vector store would
    drift out of sync the first time an indexing run was interrupted.
    """
    global _vec_available
    try:
        import sqlite_vec

        dbapi_connection.enable_load_extension(True)
        sqlite_vec.load(dbapi_connection)
        dbapi_connection.enable_load_extension(False)
        _vec_available = True
        return True
    except Exception as exc:
        if _vec_available is None:
            # Logged once. Alfred still runs: search degrades to keyword-only
            # via FTS5, which is worse but not broken.
            logger.warning(
                "sqlite-vec is unavailable (%s). Semantic search is disabled; "
                "keyword search still works.",
                exc,
            )
        _vec_available = False
        return False


def vec_available() -> bool:
    """Whether semantic search is usable. Probes once if not yet known."""
    if _vec_available is None:
        get_engine()
        with session_scope() as session:
            session.exec(text("SELECT 1"))  # type: ignore[call-overload]
    return bool(_vec_available)


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
    # After the pragmas: an extension load failure must not leave the
    # connection unconfigured.
    _load_sqlite_vec(dbapi_connection)


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


def _migration_002_vector_index(session: Session) -> None:
    """The sqlite-vec table holding chunk embeddings.

    ``rowid`` mirrors ``chunks.id``, which is what lets a vector hit be joined
    straight back to its text and line numbers. The dimension is fixed at table
    creation, so changing the embedding model means rebuilding this table - see
    ``app.indexer.store.ensure_dimensions``.
    """
    from app.config import get_settings

    if not _vec_available:
        logger.info("Skipping the vector index; sqlite-vec is not loaded.")
        return

    dims = get_settings().embedding_dimensions
    session.exec(  # type: ignore[call-overload]
        text(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING "
            f"vec0(embedding float[{dims}])"
        )
    )
    session.exec(  # type: ignore[call-overload]
        text(
            "CREATE TABLE IF NOT EXISTS index_meta ("
            "  id INTEGER PRIMARY KEY CHECK (id = 1),"
            "  embedding_model TEXT NOT NULL DEFAULT '',"
            "  dimensions INTEGER NOT NULL DEFAULT 0"
            ")"
        )
    )


MIGRATIONS: list[tuple[int, str, Callable[[Session], None]]] = [
    (1, "chunk full-text search and helper indexes", _migration_001_chunk_search),
    (2, "sqlite-vec chunk embeddings", _migration_002_vector_index),
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
