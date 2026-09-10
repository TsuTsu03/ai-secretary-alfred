"""Alfred's file tools.

Every path argument goes through ``app.security.paths``, which resolves before
checking the allowlist and applies the denylist inside allowed roots. Nothing
here opens a path the model supplied without that check.

``write_file`` is registered as mutating, so a model tool call queues a
confirmation card rather than writing anything. The write itself only happens
through ``registry.execute_approved``.
"""

from __future__ import annotations

import difflib
import logging

from app.config import Settings, get_settings
from app.indexer import embed, store
from app.llm.base import ToolSpec
from app.security import paths
from app.tools.registry import Tool, ToolError, register

logger = logging.getLogger(__name__)

MAX_TOOL_OUTPUT = 8000


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def search_files(query: str, limit: int = 6, settings: Settings | None = None) -> str:
    """Hybrid search over the index."""
    settings = settings or get_settings()
    from app.db import session_scope

    query = (query or "").strip()
    if not query:
        raise ToolError("a search needs a query")

    try:
        vector = embed.embed_query(query, settings)
    except embed.EmbeddingError as exc:
        logger.warning("Falling back to keyword-only search: %s", exc)
        vector = []

    with session_scope(settings) as session:
        hits = store.search(session, query, vector, limit=min(int(limit or 6), 12), settings=settings)

    if not hits:
        return (
            "Nothing in the index matches that. The index may not have been built yet - "
            "say so rather than guessing at an answer."
        )

    blocks = []
    for index, hit in enumerate(hits, start=1):
        blocks.append(
            f"[{index}] {hit.citation}\n{_truncate(hit.content, 1200)}"
        )
    return _truncate("\n\n".join(blocks))


def read_file(path: str, settings: Settings | None = None) -> str:
    """Read a whole file from an allowed root."""
    settings = settings or get_settings()
    try:
        resolved = paths.resolve_readable(path, settings)
    except paths.PathAccessError as exc:
        raise ToolError(str(exc)) from exc

    if not resolved.path.is_file():
        raise ToolError(f"{resolved.path} does not exist.")

    try:
        raw = resolved.path.read_bytes()
    except OSError as exc:
        raise ToolError(f"could not read {resolved.path}: {exc}") from exc

    if b"\x00" in raw[:8192]:
        raise ToolError(f"{resolved.path} is a binary file.")

    text = raw.decode("utf-8", errors="replace")
    numbered = "\n".join(
        f"{number:>5}  {line}" for number, line in enumerate(text.splitlines(), start=1)
    )
    return f"{resolved.path}\n\n{_truncate(numbered)}"


def list_directory(path: str = "", settings: Settings | None = None) -> str:
    """List a directory, or the configured roots when no path is given."""
    settings = settings or get_settings()

    if not (path or "").strip():
        roots = settings.file_roots
        if not roots:
            raise ToolError("no readable folders are configured")
        return "Readable roots:\n" + "\n".join(f"  {root}" for root in roots)

    try:
        resolved = paths.resolve_readable(path, settings)
    except paths.PathAccessError as exc:
        raise ToolError(str(exc)) from exc

    if not resolved.path.is_dir():
        raise ToolError(f"{resolved.path} is not a directory.")

    entries: list[str] = []
    try:
        for entry in sorted(resolved.path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if paths.is_denied_name(entry.name):
                continue
            if entry.is_dir():
                entries.append(f"  {entry.name}/")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                entries.append(f"  {entry.name}  ({size:,} bytes)")
    except OSError as exc:
        raise ToolError(f"could not list {resolved.path}: {exc}") from exc

    if not entries:
        return f"{resolved.path} is empty (or holds only denied files)."
    return f"{resolved.path}\n" + _truncate("\n".join(entries))


# ---------------------------------------------------------------------------
# write - mutating, and therefore gated
# ---------------------------------------------------------------------------


def write_file(path: str, content: str, settings: Settings | None = None) -> str:
    """Write a file. Only ever reached via an approved PendingAction."""
    settings = settings or get_settings()
    try:
        resolved = paths.resolve_writable(path, settings)
    except paths.PathAccessError as exc:
        raise ToolError(str(exc)) from exc

    resolved.path.parent.mkdir(parents=True, exist_ok=True)
    existed = resolved.path.is_file()
    try:
        resolved.path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"could not write {resolved.path}: {exc}") from exc

    verb = "Updated" if existed else "Created"
    return f"{verb} {resolved.path} ({len(content):,} characters)."


def _write_summary(arguments: dict) -> str:
    path = arguments.get("path", "(no path)")
    return f"Write to {path}"


def _write_detail(arguments: dict) -> str:
    """Show a diff against what is on disk, so approval is informed.

    Approving a write without seeing what changes is not consent, it is a
    formality - and this is the one card where getting it wrong loses work.
    """
    path = arguments.get("path", "")
    content = arguments.get("content", "")
    try:
        resolved = paths.resolve_writable(path)
        existing = (
            resolved.path.read_text(encoding="utf-8", errors="replace")
            if resolved.path.is_file()
            else ""
        )
        target = str(resolved.path)
    except Exception:
        existing, target = "", path

    if not existing:
        preview = content if len(content) <= 3000 else content[:3000] + "\n... [truncated]"
        return f"Create {target}\n\n{preview}"

    diff = list(
        difflib.unified_diff(
            existing.splitlines(),
            content.splitlines(),
            fromfile=f"{target} (current)",
            tofile=f"{target} (proposed)",
            lineterm="",
            n=3,
        )
    )
    if not diff:
        return f"{target} — no change."
    body = "\n".join(diff)
    return body if len(body) <= 6000 else body[:6000] + "\n... [diff truncated]"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register_file_tools() -> None:
    register(
        Tool(
            spec=ToolSpec(
                name="search_files",
                description=(
                    "Search Jansen's indexed files by meaning and by keyword. Use this "
                    "first for any question about his notes, projects, or documents. "
                    "Returns excerpts with path:line citations."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to look for, in natural language.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "How many excerpts to return (1-12). Default 6.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            run=lambda settings, query, limit=6: search_files(query, limit, settings),
        )
    )

    register(
        Tool(
            spec=ToolSpec(
                name="read_file",
                description=(
                    "Read a whole file, with line numbers. Use after search_files when "
                    "an excerpt is not enough. Absolute paths only, inside an allowed root."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path to the file."}
                    },
                    "required": ["path"],
                },
            ),
            run=lambda settings, path: read_file(path, settings),
        )
    )

    register(
        Tool(
            spec=ToolSpec(
                name="list_directory",
                description=(
                    "List a directory's contents. Call with no path to see which folders "
                    "Alfred is allowed to read at all."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path, or omit for the allowed roots.",
                        }
                    },
                    "required": [],
                },
            ),
            run=lambda settings, path="": list_directory(path, settings),
        )
    )

    register(
        Tool(
            spec=ToolSpec(
                name="write_file",
                description=(
                    "Propose writing a file. This does NOT write immediately - it puts a "
                    "confirmation card in front of Jansen showing a diff, and he decides. "
                    "Never tell him a file has been written from this call alone."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path to write."},
                        "content": {"type": "string", "description": "The full file contents."},
                    },
                    "required": ["path", "content"],
                },
            ),
            run=lambda settings, path, content: write_file(path, content, settings),
            mutating=True,
            summarize=_write_summary,
            describe=_write_detail,
        )
    )
