"""Walking the allowed roots and keeping the index current.

Indexing is incremental and content-addressed. Editors rewrite files on every
save, so mtime alone would reindex a whole project because one file was opened
and closed. Hashing the bytes is cheap next to embedding them, so the hash is
what decides.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import select

from app.config import Settings, get_settings
from app.db import session_scope
from app.indexer import embed, store
from app.indexer.chunker import chunk_text
from app.models import IndexedFile
from app.security import paths

logger = logging.getLogger(__name__)


@dataclass
class IndexProgress:
    running: bool = False
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    removed: int = 0
    failed: int = 0
    total: int = 0
    current: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        elapsed = (self.finished_at or time.time()) - self.started_at if self.started_at else 0.0
        return {
            "running": self.running,
            "scanned": self.scanned,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "removed": self.removed,
            "failed": self.failed,
            "total": self.total,
            "current": self.current,
            "elapsed_seconds": round(elapsed, 1),
            "error": self.error,
        }


_progress = IndexProgress()
_lock = threading.Lock()


def progress() -> IndexProgress:
    return _progress


def _read_text(path: Path, settings: Settings) -> str:
    """Read a file as text, tolerating whatever encoding it happens to be.

    ``errors="replace"`` rather than a decode failure: a stray byte in a log
    should cost one character, not the whole file.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"could not read: {exc}") from exc

    # A NUL in the first block means binary. Extension checks miss plenty.
    if b"\x00" in raw[:8192]:
        raise ValueError("binary file")

    text = raw.decode("utf-8", errors="replace")
    if len(text) > settings.index_max_chars:
        # Truncate rather than skip: the head of a large file is still useful,
        # and skipping means Alfred cannot see it at all.
        text = text[: settings.index_max_chars]
    return text


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def index_once(settings: Settings | None = None, rebuild: bool = False) -> IndexProgress:
    """Bring the index up to date. Blocking; run it on the worker thread."""
    settings = settings or get_settings()

    with _lock:
        if _progress.running:
            return _progress
        _progress.__init__()  # type: ignore[misc]
        _progress.running = True
        _progress.started_at = time.time()

    try:
        with session_scope(settings) as session:
            if rebuild or store.ensure_dimensions(session, settings):
                logger.info("Rebuilding the index from scratch.")
                store.clear_index(session)

        # Walking is cheap; do it once up front so progress has a denominator.
        try:
            found = list(paths.walk_roots(settings))
        except paths.PathAccessError as exc:
            _progress.error = str(exc)
            return _progress

        _progress.total = len(found)
        logger.info("Indexing %d files...", len(found))

        seen_paths: set[str] = set()

        for resolved in found:
            _progress.scanned += 1
            path_text = str(resolved.path)
            seen_paths.add(path_text)
            _progress.current = path_text

            try:
                content = _read_text(resolved.path, settings)
            except ValueError:
                _progress.skipped += 1
                continue

            digest = _hash(content)

            with session_scope(settings) as session:
                existing = session.exec(
                    select(IndexedFile).where(IndexedFile.path == path_text)
                ).first()

                # The whole point of hashing: an editor's save-on-open rewrites
                # mtime without changing a byte, and re-embedding a project for
                # that would cost minutes.
                if existing and existing.content_hash == digest:
                    _progress.skipped += 1
                    continue

                chunks = chunk_text(
                    content,
                    max_chars=settings.chunk_chars,
                    overlap_chars=settings.chunk_overlap_chars,
                )
                if not chunks:
                    _progress.skipped += 1
                    continue

                try:
                    vectors = embed.embed_documents([c.text for c in chunks], settings)
                except embed.EmbeddingError as exc:
                    logger.warning("Could not embed %s: %s", path_text, exc)
                    _progress.failed += 1
                    continue

                if existing is not None:
                    assert existing.id is not None
                    store.remove_file(session, existing.id)
                    record = existing
                else:
                    record = IndexedFile(path=path_text)

                stat = resolved.path.stat()
                record.root = str(resolved.root)
                record.suffix = resolved.path.suffix.lower()
                record.size_bytes = stat.st_size
                record.content_hash = digest
                record.mtime = stat.st_mtime
                record.chunk_count = len(chunks)
                session.add(record)
                session.flush()
                assert record.id is not None

                store.store_chunks(session, record.id, path_text, chunks, vectors)
                _progress.indexed += 1

        # Anything indexed but no longer on disk (or newly denied) has to go,
        # or Alfred will confidently cite a file that is not there.
        with session_scope(settings) as session:
            known = session.exec(select(IndexedFile)).all()
            for record in known:
                if record.path in seen_paths:
                    continue
                assert record.id is not None
                store.remove_file(session, record.id)
                session.delete(record)
                _progress.removed += 1

        logger.info(
            "Index complete: %d indexed, %d unchanged, %d removed, %d failed.",
            _progress.indexed, _progress.skipped, _progress.removed, _progress.failed,
        )
    except Exception as exc:
        logger.exception("Indexing failed")
        _progress.error = str(exc)
    finally:
        _progress.running = False
        _progress.current = ""
        _progress.finished_at = time.time()

    return _progress


def index_in_background(settings: Settings | None = None, rebuild: bool = False) -> bool:
    """Start an indexing run on a daemon thread. False if one is already going."""
    if _progress.running:
        return False
    thread = threading.Thread(
        target=index_once, args=(settings, rebuild), name="alfred-indexer", daemon=True
    )
    thread.start()
    return True
