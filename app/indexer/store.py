"""Storing and searching chunks.

Search is hybrid, because the two halves fail in opposite directions:

* **Vectors** find "the quotation for the wedding client" when the note says
  "budgetary estimate for Erica and Gabriel". They are useless at exact tokens.
* **FTS5** finds ``ALFRED_TAILSCALE_HOSTNAME``, an invoice number, or a
  filename. It finds nothing if the wording differs at all.

Results are merged with Reciprocal Rank Fusion, which needs no score
calibration between the two - a genuine convenience, since a cosine distance
and a BM25 rank are not comparable quantities.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.db import FTS_TABLE, VEC_TABLE, vec_available
from app.models import Chunk, IndexedFile

logger = logging.getLogger(__name__)

# RRF's constant. 60 is the value from the original paper and behaves well
# without tuning; it damps the influence of any single ranking's top hit.
RRF_K = 60

# Plain RRF assumes both rankings are reasonable. Over 100k chunks that is
# false: for a rare identifier the nearest vector neighbour is still unrelated,
# and unweighted fusion promoted it level with an exact keyword hit. Keyword
# evidence is the more trustworthy of the two when it exists at all.
VECTOR_WEIGHT = 1.0
KEYWORD_WEIGHT = 1.4

# Vectors are L2-normalised, so distance runs 0..2 and maps to cosine as
# d = sqrt(2 - 2cos). 1.15 is roughly cosine 0.34 - below that the neighbour is
# not about the same subject and only adds noise to the fusion.
MAX_VECTOR_DISTANCE = 1.15

# Words too common to narrow anything down. Kept small and bilingual: Jansen
# writes Taglish, so dropping only English stopwords would leave "ang", "ng"
# and "para" doing the same damage.
STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
        "is", "are", "was", "were", "be", "been", "it", "its", "this", "that",
        "what", "which", "who", "how", "when", "where", "why", "do", "does",
        "did", "my", "me", "i", "you", "your", "about", "from", "at", "by",
        "as", "can", "could", "would", "should", "have", "has", "had", "not",
        "ang", "ng", "sa", "na", "ay", "mga", "para", "ako", "ko", "mo",
        "yung", "ba", "po", "may", "kung", "ito", "iyon", "niya", "nila",
    }
)


@dataclass(slots=True)
class SearchHit:
    path: str
    relative: str
    content: str
    start_line: int
    end_line: int
    score: float
    chunk_id: int

    @property
    def citation(self) -> str:
        """What Alfred quotes back. A citation Jansen cannot open is useless."""
        if self.start_line == self.end_line:
            return f"{self.path}:{self.start_line}"
        return f"{self.path}:{self.start_line}-{self.end_line}"


def serialize(vector: list[float]) -> bytes:
    """Pack a float list the way sqlite-vec expects."""
    return struct.pack(f"{len(vector)}f", *vector)


def _identity(settings: Settings) -> str:
    from app.indexer import embed

    return embed.index_identity(settings)


def ensure_dimensions(session: Session, settings: Settings | None = None) -> bool:
    """Detect an embedding-model change and report whether a rebuild is needed.

    The vec0 table's dimension is fixed at creation. Swapping models without
    rebuilding produces either an insert error or - worse, if dimensions happen
    to match - silently meaningless similarity scores.
    """
    settings = settings or get_settings()
    if not vec_available():
        return False

    row = session.exec(  # type: ignore[call-overload]
        text("SELECT embedding_model, dimensions FROM index_meta WHERE id = 1")
    ).first()

    if row is None:
        session.exec(  # type: ignore[call-overload]
            text(
                "INSERT OR REPLACE INTO index_meta (id, embedding_model, dimensions) "
                "VALUES (1, :m, :d)"
            ).bindparams(m=_identity(settings), d=settings.embedding_dimensions)
        )
        return False

    stored_model, stored_dims = row[0], int(row[1])
    if stored_model == _identity(settings) and stored_dims == settings.embedding_dimensions:
        return False

    logger.warning(
        "Embedding identity changed (%s/%d -> %s/%d). The index must be rebuilt.",
        stored_model, stored_dims, _identity(settings), settings.embedding_dimensions,
    )
    session.exec(  # type: ignore[call-overload]
        text(
            "INSERT OR REPLACE INTO index_meta (id, embedding_model, dimensions) "
            "VALUES (1, :m, :d)"
        ).bindparams(m=_identity(settings), d=settings.embedding_dimensions)
    )
    return True


def clear_index(session: Session) -> None:
    """Drop every chunk, vector, and search row. Files are not touched."""
    session.exec(text(f"DELETE FROM {FTS_TABLE}"))  # type: ignore[call-overload]
    if vec_available():
        session.exec(text(f"DELETE FROM {VEC_TABLE}"))  # type: ignore[call-overload]
    session.exec(text("DELETE FROM chunks"))  # type: ignore[call-overload]
    session.exec(text("DELETE FROM indexed_files"))  # type: ignore[call-overload]


def remove_file(session: Session, file_id: int) -> None:
    """Remove one file's chunks from every index."""
    ids = [row[0] for row in session.exec(  # type: ignore[call-overload]
        text("SELECT id FROM chunks WHERE file_id = :f").bindparams(f=file_id)
    ).all()]
    for chunk_id in ids:
        session.exec(  # type: ignore[call-overload]
            text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :r").bindparams(r=chunk_id)
        )
        if vec_available():
            session.exec(  # type: ignore[call-overload]
                text(f"DELETE FROM {VEC_TABLE} WHERE rowid = :r").bindparams(r=chunk_id)
            )
    session.exec(  # type: ignore[call-overload]
        text("DELETE FROM chunks WHERE file_id = :f").bindparams(f=file_id)
    )


def store_chunks(
    session: Session,
    file_id: int,
    path: str,
    chunks: list,
    vectors: list[list[float]],
) -> int:
    """Write chunks with their embeddings. Caller owns the transaction."""
    written = 0
    for chunk, vector in zip(chunks, vectors, strict=False):
        row = Chunk(
            file_id=file_id,
            ordinal=chunk.ordinal,
            content=chunk.text,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
        )
        session.add(row)
        session.flush()  # assigns row.id, which both indexes key on
        assert row.id is not None

        session.exec(  # type: ignore[call-overload]
            text(
                f"INSERT INTO {FTS_TABLE} (rowid, content, path) VALUES (:r, :c, :p)"
            ).bindparams(r=row.id, c=chunk.text, p=path)
        )
        if vec_available() and vector:
            session.exec(  # type: ignore[call-overload]
                text(
                    f"INSERT INTO {VEC_TABLE} (rowid, embedding) VALUES (:r, :e)"
                ).bindparams(r=row.id, e=serialize(vector))
            )
        written += 1
    return written


def _fetch(session: Session, chunk_ids: list[int]) -> dict[int, tuple[Chunk, IndexedFile]]:
    if not chunk_ids:
        return {}
    rows = session.exec(
        select(Chunk, IndexedFile)
        .join(IndexedFile, IndexedFile.id == Chunk.file_id)  # type: ignore[arg-type]
        .where(Chunk.id.in_(chunk_ids))  # type: ignore[union-attr]
    ).all()
    return {chunk.id: (chunk, file) for chunk, file in rows if chunk.id is not None}


def _vector_ranking(session: Session, vector: list[float], limit: int) -> list[int]:
    """Nearest neighbours, with the obviously unrelated ones dropped.

    Without the ceiling, the nearest neighbour to a rare identifier is still
    returned at rank 0 and fusion treats it as strong evidence. That is how a
    search for ALFRED_TAILSCALE_HOSTNAME put an unrelated TypeScript file above
    the config module that defines it.
    """
    if not vec_available() or not vector:
        return []
    try:
        rows = session.exec(  # type: ignore[call-overload]
            text(
                f"SELECT rowid, distance FROM {VEC_TABLE} "
                "WHERE embedding MATCH :e AND k = :k ORDER BY distance"
            ).bindparams(e=serialize(vector), k=limit)
        ).all()
    except Exception as exc:
        logger.warning("Vector search failed: %s", exc)
        return []

    return [int(row[0]) for row in rows if float(row[1]) <= MAX_VECTOR_DISTANCE]


def _terms(query: str) -> list[str]:
    """Searchable terms: punctuation stripped, stopwords dropped.

    FTS5 treats punctuation as syntax, so a raw query ending in a question mark
    is a syntax error rather than a search.
    """
    cleaned = "".join(c if c.isalnum() else " " for c in query)
    words = [w for w in cleaned.split() if len(w) > 1]
    meaningful = [w for w in words if w.lower() not in STOPWORDS]
    # A query of nothing but stopwords should still search for something.
    return meaningful or words


def _fts_query(session: Session, expression: str, limit: int) -> list[int]:
    try:
        rows = session.exec(  # type: ignore[call-overload]
            text(
                f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :q "
                "ORDER BY rank LIMIT :k"
            ).bindparams(q=expression, k=limit)
        ).all()
        return [int(row[0]) for row in rows]
    except Exception as exc:
        logger.warning("Keyword search failed (%s): %s", expression[:80], exc)
        return []


def _keyword_ranking(session: Session, query: str, limit: int) -> list[int]:
    """Chunks matching every term first, then chunks matching any of them.

    OR alone was the bug: "how does the confirmation gate stop a write" matched
    anything containing "write", which across 100k chunks is most of a codebase.
    Running AND first puts documents carrying the whole phrase above documents
    that merely share one word with it.
    """
    terms = _terms(query)
    if not terms:
        return []
    quoted = ['"' + term + '"' for term in terms]

    ordered: list[int] = []
    seen: set[int] = set()

    expressions = [" OR ".join(quoted)]
    if len(quoted) > 1:
        expressions.insert(0, " AND ".join(quoted))

    for expression in expressions:
        for chunk_id in _fts_query(session, expression, limit):
            if chunk_id not in seen:
                seen.add(chunk_id)
                ordered.append(chunk_id)
        if len(ordered) >= limit:
            break
    return ordered[:limit]


def search(
    session: Session,
    query: str,
    query_vector: list[float] | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
) -> list[SearchHit]:
    """Hybrid search over the index, best first."""
    settings = settings or get_settings()
    limit = limit or settings.search_results
    # Over-fetch from each ranking so the fusion has something to work with.
    depth = max(limit * 4, 20)

    rankings = [
        (_vector_ranking(session, query_vector or [], depth), VECTOR_WEIGHT),
        (_keyword_ranking(session, query, depth), KEYWORD_WEIGHT),
    ]

    # Weighted Reciprocal Rank Fusion: a chunk found by both methods outranks
    # one found brilliantly by only one, since agreement between two unrelated
    # signals is the strongest evidence available. The weights decide it when
    # only one ranking has an opinion at all.
    scores: dict[int, float] = {}
    for ranking, weight in rankings:
        for position, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + position + 1)

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    loaded = _fetch(session, [chunk_id for chunk_id, _ in ordered])

    hits: list[SearchHit] = []
    for chunk_id, score in ordered:
        found = loaded.get(chunk_id)
        if found is None:
            continue
        chunk, file = found
        relative = file.path
        if file.root and file.path.startswith(file.root):
            relative = file.path[len(file.root) :].lstrip("\\/")
        hits.append(
            SearchHit(
                path=file.path,
                relative=relative,
                content=chunk.content,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                score=score,
                chunk_id=chunk_id,
            )
        )
    return hits


def stats(session: Session) -> dict:
    files = session.exec(text("SELECT COUNT(*) FROM indexed_files")).first()  # type: ignore[call-overload]
    chunks = session.exec(text("SELECT COUNT(*) FROM chunks")).first()  # type: ignore[call-overload]
    return {
        "files": int(files[0]) if files else 0,
        "chunks": int(chunks[0]) if chunks else 0,
        "semantic": vec_available(),
    }
