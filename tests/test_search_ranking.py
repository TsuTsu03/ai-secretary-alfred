"""Tests for how search results are ranked.

These pin the fixes made after the first full-corpus spot check, where all the
top scores came back as 0.0164/0.0161 - the signature of two rankings that
never agree, fused without weights, so a semantically unrelated chunk sat level
with an exact keyword match.
"""

from __future__ import annotations

import math

import pytest

from app.indexer import embed
from app.indexer.store import (
    KEYWORD_WEIGHT,
    MAX_VECTOR_DISTANCE,
    RRF_K,
    VECTOR_WEIGHT,
    _terms,
)

# ── query terms ──────────────────────────────────────────────────────────


def test_punctuation_is_stripped() -> None:
    """FTS5 reads punctuation as syntax, so a question mark is a syntax error."""
    assert "?" not in " ".join(_terms("where is the quotation?"))
    assert _terms("what's the plan?") == ["plan"]


def test_english_stopwords_are_dropped() -> None:
    assert _terms("how does the confirmation gate stop a write") == [
        "confirmation", "gate", "stop", "write",
    ]


def test_tagalog_stopwords_are_dropped_too() -> None:
    """Dropping only English would leave ang/ng/para doing the same damage."""
    assert _terms("ano ang plano para sa Smiley dental clinic") == [
        "ano", "plano", "Smiley", "dental", "clinic",
    ]


def test_a_query_of_pure_stopwords_still_searches_for_something() -> None:
    assert _terms("what is it") == ["what", "is", "it"]


def test_single_characters_are_dropped() -> None:
    assert _terms("a b plan") == ["plan"]


def test_identifiers_survive_intact() -> None:
    """An underscore is punctuation to FTS5, so the token splits - and each
    half is still far rarer than any English word, which is what matters."""
    assert _terms("ALFRED_TAILSCALE_HOSTNAME") == ["ALFRED", "TAILSCALE", "HOSTNAME"]


# ── fusion weighting ─────────────────────────────────────────────────────


def test_keyword_outweighs_vector_at_equal_rank() -> None:
    """The exact fix for the spot-check failure: when the two rankings share no
    results, the keyword hit must not tie with the semantic one."""
    keyword_top = KEYWORD_WEIGHT / (RRF_K + 1)
    vector_top = VECTOR_WEIGHT / (RRF_K + 1)
    assert keyword_top > vector_top


def test_agreement_still_beats_either_ranking_alone() -> None:
    """Weighting must not defeat the point of fusing in the first place."""
    both = VECTOR_WEIGHT / (RRF_K + 1) + KEYWORD_WEIGHT / (RRF_K + 3)
    keyword_only = KEYWORD_WEIGHT / (RRF_K + 1)
    assert both > keyword_only


# ── vector distance ──────────────────────────────────────────────────────


def test_distance_ceiling_matches_a_sane_cosine() -> None:
    """d = sqrt(2 - 2cos) for unit vectors. The ceiling should sit near 0.34,
    below which a neighbour is not about the same subject."""
    cosine = 1 - (MAX_VECTOR_DISTANCE**2) / 2
    assert 0.25 < cosine < 0.45


# ── normalisation ────────────────────────────────────────────────────────


def test_normalise_produces_unit_vectors() -> None:
    """Unnormalised, L2 distance measures vector length as much as meaning."""
    normalised = embed._normalise([3.0, 4.0])
    assert math.isclose(math.sqrt(sum(v * v for v in normalised)), 1.0, rel_tol=1e-6)
    assert normalised == pytest.approx([0.6, 0.8])


def test_normalise_preserves_direction() -> None:
    scaled = embed._normalise([10.0, 0.0, 0.0])
    assert scaled == pytest.approx([1.0, 0.0, 0.0])


def test_normalise_leaves_a_zero_vector_alone() -> None:
    """Dividing by zero here would poison the whole index with NaNs."""
    assert embed._normalise([0.0, 0.0]) == [0.0, 0.0]


def test_index_identity_carries_the_revision() -> None:
    """A revision bump has to invalidate stored vectors, or old unnormalised
    ones sit alongside new normalised ones and neither ranks correctly."""
    from app.config import Settings

    settings = Settings(ALFRED_DATA_DIR="x")  # type: ignore[call-arg]
    identity = embed.index_identity(settings)
    assert settings.embedding_model in identity
    assert embed.EMBEDDING_REVISION in identity
