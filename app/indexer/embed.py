"""Local embeddings.

Runs on CPU through fastembed's ONNX runtime. Two reasons it is not on the GPU:
Whisper is already there, and an embedding pass over a whole home directory
would evict the Whisper weights repeatedly during a conversation.

The model is multilingual on purpose. Jansen writes Taglish, and an English-only
embedding model does not fail loudly on Tagalog - it just quietly fails to match
it, which looks like "Alfred could not find the note" rather than like a bug.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Embedding failed. The message is safe to show the user."""


class _Embedder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._name = ""

    def load(self, settings: Settings):
        if self._model is not None and self._name == settings.embedding_model:
            return self._model
        with self._lock:
            if self._model is not None and self._name == settings.embedding_model:
                return self._model

            from fastembed import TextEmbedding

            logger.info("Loading embedding model %s ...", settings.embedding_model)
            try:
                self._model = TextEmbedding(
                    model_name=settings.embedding_model,
                    cache_dir=str(settings.models_dir / "embeddings"),
                )
            except Exception as exc:
                raise EmbeddingError(
                    f"Could not load the embedding model {settings.embedding_model!r}: {exc}"
                ) from exc
            self._name = settings.embedding_model
            logger.info("Embedding model ready.")
            return self._model

    @property
    def loaded(self) -> bool:
        return self._model is not None


_embedder = _Embedder()


def embed_documents(texts: Iterable[str], settings: Settings | None = None) -> list[list[float]]:
    """Embed chunks for storage. Blocking; call it off the event loop."""
    settings = settings or get_settings()
    items = list(texts)
    if not items:
        return []
    model = _embedder.load(settings)
    try:
        return [vector.tolist() for vector in model.embed(items)]
    except Exception as exc:
        raise EmbeddingError(f"Could not embed those documents: {exc}") from exc


def embed_query(text: str, settings: Settings | None = None) -> list[float]:
    """Embed a search query.

    fastembed exposes ``query_embed`` for models that use a different prefix for
    queries than for documents (the E5 family does). Falling back to plain
    ``embed`` keeps models without that distinction working.
    """
    settings = settings or get_settings()
    model = _embedder.load(settings)
    try:
        if hasattr(model, "query_embed"):
            for vector in model.query_embed([text]):
                return vector.tolist()
        for vector in model.embed([text]):
            return vector.tolist()
    except Exception as exc:
        raise EmbeddingError(f"Could not embed that query: {exc}") from exc
    raise EmbeddingError("The embedding model returned nothing.")


def dimensions(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    return settings.embedding_dimensions


def describe(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    return {
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
        "loaded": _embedder.loaded,
    }
