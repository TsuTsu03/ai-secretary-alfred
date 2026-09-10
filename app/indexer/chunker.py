"""Split a document into retrievable pieces.

Chunking decides what Alfred can find. Two rules shape it:

* **Split on structure before length.** A Markdown heading or a blank line is a
  real boundary; the 1,200th character is an arbitrary one. Cutting mid-sentence
  produces chunks that embed poorly and read badly when quoted back.
* **Keep line numbers.** A citation Jansen cannot open is barely a citation, so
  every chunk carries the line range it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A Markdown heading, a code fence, or a blank line. These are where a document
# actually changes subject.
_BOUNDARY = re.compile(r"^(#{1,6}\s|```|\s*$)")


@dataclass(slots=True)
class Chunk:
    text: str
    start_line: int
    end_line: int
    ordinal: int


def _blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Group lines into structural blocks, each tagged with its start line.

    Code fences are kept whole: splitting inside one produces a chunk that is
    half a function and matches nothing anybody would search for.
    """
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start = 1
    in_fence = False

    for index, line in enumerate(lines, start=1):
        fence = line.lstrip().startswith("```")
        if fence:
            in_fence = not in_fence
            current.append(line)
            # A closing fence ends the block; an opening one continues it.
            if not in_fence:
                blocks.append((start, current))
                current, start = [], index + 1
            continue

        if not in_fence and _BOUNDARY.match(line) and current:
            blocks.append((start, current))
            current, start = [], index
            # A heading opens the next block rather than closing the last one.
            if line.strip():
                current.append(line)
                continue
            continue

        if not current:
            start = index
        current.append(line)

    if current:
        blocks.append((start, current))
    return blocks


def chunk_text(
    content: str,
    max_chars: int = 1200,
    overlap_chars: int = 180,
) -> list[Chunk]:
    """Split `content` into overlapping chunks that respect structure."""
    if not content.strip():
        return []

    lines = content.splitlines()
    chunks: list[Chunk] = []

    buffer: list[str] = []
    buffer_start = 1
    ordinal = 0

    def flush(end_line: int) -> None:
        nonlocal buffer, buffer_start, ordinal
        text = "\n".join(buffer).strip()
        if text:
            chunks.append(
                Chunk(text=text, start_line=buffer_start, end_line=end_line, ordinal=ordinal)
            )
            ordinal += 1
        buffer = []

    for start, block in _blocks(lines):
        block_text = "\n".join(block)

        # A single block longer than the limit (a minified file, a long table)
        # has no internal structure to respect, so it is cut by length.
        if len(block_text) > max_chars:
            if buffer:
                flush(start - 1)
            for offset in range(0, len(block_text), max_chars - overlap_chars):
                piece = block_text[offset : offset + max_chars]
                if not piece.strip():
                    continue
                # Line numbers within an over-long block are approximate; the
                # block's own range is the honest answer.
                chunks.append(
                    Chunk(
                        text=piece.strip(),
                        start_line=start,
                        end_line=start + len(block) - 1,
                        ordinal=ordinal,
                    )
                )
                ordinal += 1
            buffer_start = start + len(block)
            continue

        current_len = sum(len(line) + 1 for line in buffer)
        if buffer and current_len + len(block_text) > max_chars:
            flush(start - 1)
            # Carry the tail of the previous chunk so a sentence spanning the
            # boundary is still findable from either side.
            if overlap_chars > 0 and chunks:
                tail = chunks[-1].text[-overlap_chars:]
                buffer = [tail]
                buffer_start = max(1, start - tail.count("\n") - 1)
            else:
                buffer_start = start

        if not buffer:
            buffer_start = start
        buffer.extend(block)

    flush(len(lines))
    return chunks
