"""Database tables.

Importing this module is what registers tables on ``SQLModel.metadata``, so
``app.db`` imports it explicitly rather than relying on import order.

The one table worth reading carefully is :class:`PendingAction`. It is the
mechanism behind Alfred's central safety rule: a model tool call that would
change something on disk, in the calendar, or in the mailbox never executes
directly. It writes a row here, the UI renders it as a card, and only Jansen's
approval moves it to ``approved`` and lets the worker carry it out.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class SchemaVersion(SQLModel, table=True):
    __tablename__ = "schema_version"

    id: int = Field(default=1, primary_key=True)
    version: int = Field(default=0)
    applied_at: datetime = Field(default_factory=_now)


class Role(StrEnum):
    USER = "user"
    ALFRED = "alfred"
    TOOL = "tool"
    SYSTEM = "system"


class InputMode(StrEnum):
    TEXT = "text"
    VOICE = "voice"


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(default="")
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    archived: bool = Field(default=False, index=True)


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversations.id", index=True)
    role: Role = Field(index=True)
    content: str = Field(default="")
    # Which device the message came from, for the "you asked this on your phone"
    # affordance. Not a security control.
    input_mode: InputMode = Field(default=InputMode.TEXT)
    # Populated for voice turns so the clip can be replayed.
    audio_filename: str = Field(default="")
    # Provider and model that produced an assistant turn, for debugging which
    # free tier answered and whether a fallback fired.
    provider: str = Field(default="")
    model: str = Field(default="")
    # JSON blob of tool calls issued during this turn. Stored as text because
    # nothing queries inside it.
    tool_calls_json: str = Field(default="")
    created_at: datetime = Field(default_factory=_now, index=True)


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    EXECUTED = "executed"
    FAILED = "failed"
    EXPIRED = "expired"


class PendingAction(SQLModel, table=True):
    """A side effect awaiting Jansen's explicit approval.

    Nothing that writes a file, changes the calendar, or sends mail may run
    without a row here reaching ``approved``. The tool layer creates the row and
    returns a description to the model; it does not perform the action.
    """

    __tablename__ = "pending_actions"

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversations.id", index=True)
    # Tool that requested it, e.g. "files.write_file" or "calendar.create_event".
    tool_name: str = Field(index=True)
    # Human-readable one-liner rendered on the confirmation card.
    summary: str = Field(default="")
    # Fuller description: the diff, the event details, the draft body.
    detail: str = Field(default="")
    # JSON arguments replayed verbatim when approved. Never re-derived from the
    # model after approval, so what Jansen sees is exactly what runs.
    arguments_json: str = Field(default="")
    status: ActionStatus = Field(default=ActionStatus.PENDING, index=True)
    result: str = Field(default="")
    created_at: datetime = Field(default_factory=_now, index=True)
    resolved_at: datetime | None = Field(default=None)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Job(SQLModel, table=True):
    """Background work: indexing, briefing generation, transcription."""

    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)
    payload_json: str = Field(default="")
    status: JobStatus = Field(default=JobStatus.QUEUED, index=True)
    progress: float = Field(default=0.0)
    message: str = Field(default="")
    error: str = Field(default="")
    created_at: datetime = Field(default_factory=_now, index=True)
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)


class IndexedFile(SQLModel, table=True):
    """One file Alfred has read and chunked.

    ``content_hash`` is what makes reindexing cheap: the watcher recomputes it
    on change and skips files whose bytes are unchanged despite a new mtime,
    which happens constantly with editors that rewrite on save.
    """

    __tablename__ = "indexed_files"

    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(index=True, unique=True)
    root: str = Field(default="")
    suffix: str = Field(default="", index=True)
    size_bytes: int = Field(default=0)
    content_hash: str = Field(default="", index=True)
    mtime: float = Field(default=0.0)
    chunk_count: int = Field(default=0)
    indexed_at: datetime = Field(default_factory=_now, index=True)


class Chunk(SQLModel, table=True):
    """A slice of an indexed file, with its offsets for citation."""

    __tablename__ = "chunks"

    id: int | None = Field(default=None, primary_key=True)
    file_id: int = Field(foreign_key="indexed_files.id", index=True)
    ordinal: int = Field(default=0)
    content: str = Field(default="")
    start_line: int = Field(default=0)
    end_line: int = Field(default=0)


class Device(SQLModel, table=True):
    """A paired client, recorded so push notifications have somewhere to go."""

    __tablename__ = "devices"

    id: int | None = Field(default=None, primary_key=True)
    label: str = Field(default="")
    user_agent: str = Field(default="")
    push_endpoint: str = Field(default="")
    push_p256dh: str = Field(default="")
    push_auth: str = Field(default="")
    created_at: datetime = Field(default_factory=_now)
    last_seen_at: datetime = Field(default_factory=_now, index=True)


class Briefing(SQLModel, table=True):
    """A generated daily briefing, kept so the phone can open it from a push."""

    __tablename__ = "briefings"

    id: int | None = Field(default=None, primary_key=True)
    for_date: str = Field(index=True)
    content: str = Field(default="")
    delivered: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
