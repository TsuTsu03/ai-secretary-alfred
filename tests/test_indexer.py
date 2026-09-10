"""Tests for chunking and the index's exclusion rules.

No model loading here - these pin the decisions around retrieval, not the
retrieval itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.indexer.chunker import chunk_text
from app.security.paths import is_generated_name, is_readable_file, walk_roots

# ── chunking ─────────────────────────────────────────────────────────────


def test_empty_content_yields_nothing() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_document_is_one_chunk() -> None:
    chunks = chunk_text("# Title\n\nA short note about the wedding quotation.")
    assert len(chunks) == 1
    assert "wedding quotation" in chunks[0].text


def test_chunks_carry_line_numbers() -> None:
    """A citation Jansen cannot open is barely a citation."""
    content = "\n".join(f"line {n}" for n in range(1, 400))
    chunks = chunk_text(content, max_chars=400, overlap_chars=0)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.start_line >= 1
        assert chunk.end_line >= chunk.start_line


def test_long_document_splits() -> None:
    content = "\n\n".join(f"Paragraph {n}. " + "word " * 60 for n in range(40))
    chunks = chunk_text(content, max_chars=1200, overlap_chars=180)
    assert len(chunks) > 3
    assert all(chunk.text.strip() for chunk in chunks)


def test_ordinals_are_sequential() -> None:
    content = "\n\n".join(f"Section {n}\n" + "text " * 100 for n in range(12))
    chunks = chunk_text(content, max_chars=800, overlap_chars=100)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_a_single_oversized_block_is_still_split() -> None:
    """A minified file has no structure to respect, so length has to win."""
    chunks = chunk_text("x" * 10_000, max_chars=1000, overlap_chars=100)
    assert len(chunks) > 1
    assert all(len(c.text) <= 1000 for c in chunks)


def test_code_fences_are_not_split_mid_block() -> None:
    """Half a function embeds badly and matches nothing anyone would search."""
    content = "Intro paragraph.\n\n```python\n" + "\n".join(
        f"    step_{n}()" for n in range(12)
    ) + "\n```\n\nClosing paragraph."
    chunks = chunk_text(content, max_chars=4000, overlap_chars=0)
    fenced = [c for c in chunks if "```" in c.text]
    for chunk in fenced:
        assert chunk.text.count("```") % 2 == 0, "a fence was left unclosed"


# ── what gets indexed ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "app.min.js",
     "styles.min.css", "bundle.js.map", "Cargo.lock", "go.sum"],
)
def test_generated_files_are_skipped(name: str) -> None:
    """These dominated a real run: a few hundred chunks of nothing, per project."""
    assert is_generated_name(name)


@pytest.mark.parametrize(
    "name", ["package.json", "main.js", "styles.css", "notes.md", "lockfile-notes.md"]
)
def test_real_files_are_not_mistaken_for_generated(name: str) -> None:
    assert not is_generated_name(name)


def test_generated_files_are_excluded_from_the_walk(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (root / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    (root / "app.min.js").write_text("var a=1", encoding="utf-8")
    (root / "index.js").write_text("console.log(1)", encoding="utf-8")

    settings = Settings(
        ALFRED_FILE_ROOTS=str(root), ALFRED_DATA_DIR=str(tmp_path / "d")
    )  # type: ignore[call-arg]
    found = {r.path.name for r in walk_roots(settings)}
    assert found == {"package.json", "index.js"}


def test_binary_suffixes_are_not_readable(tmp_path: Path) -> None:
    settings = Settings(
        ALFRED_FILE_ROOTS=str(tmp_path), ALFRED_DATA_DIR=str(tmp_path / "d")
    )  # type: ignore[call-arg]
    for name in ("photo.png", "archive.zip", "model.onnx", "video.mp4"):
        target = tmp_path / name
        target.write_bytes(b"\x00\x01")
        assert not is_readable_file(target, settings)
