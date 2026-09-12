"""The daily briefing.

Alfred opens the day rather than waiting to be asked. He gathers the facts
himself - calendar, inbox, what changed in the projects - and then writes them
up in his own voice.

Two rules shape the result:

* **Gather first, then write.** The facts are collected by calling the tools
  directly rather than letting the model decide what to look at. A briefing
  that silently skipped the calendar because the model did not think to check
  is worse than no briefing, because it looks complete.
* **Say when something is missing.** If the mailbox could not be read, the
  briefing says so. An omission that reads as "nothing to report" is a lie by
  layout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings, get_settings
from app.llm.base import ChatMessage
from app.llm.router import Router
from app.persona.prompt import build_system_prompt, wrap_untrusted

logger = logging.getLogger(__name__)

# How far back to look for project activity. A day misses the weekend; a week
# buries today under Monday.
ACTIVITY_WINDOW_HOURS = 36


@dataclass
class Gathered:
    """Raw material, before Alfred phrases any of it."""

    for_date: str
    calendar: str = ""
    mail: str = ""
    activity: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.calendar or self.mail or self.activity)


def _today(settings: Settings) -> datetime:
    try:
        return datetime.now(ZoneInfo(settings.timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(UTC)


def gather(settings: Settings | None = None) -> Gathered:
    """Collect the day's facts. Never raises; failures become `problems`."""
    settings = settings or get_settings()
    now = _today(settings)
    data = Gathered(for_date=now.strftime("%Y-%m-%d"))

    from app.integrations import google_oauth

    if google_oauth.is_connected(settings):
        from app.tools import calendar as cal
        from app.tools import gmail

        try:
            data.calendar = cal.list_events(2, settings)
        except Exception as exc:
            data.problems.append(f"Could not read the calendar: {exc}")

        try:
            # Unread only, and recent. A briefing that lists a 400-message
            # backlog is a wall, not a briefing.
            data.mail = gmail.search_mail("in:inbox is:unread newer_than:2d", 10, settings)
        except Exception as exc:
            data.problems.append(f"Could not read the mailbox: {exc}")
    else:
        data.problems.append("Google is not connected, so there is no calendar or mail.")

    try:
        data.activity = _recent_activity(settings)
    except Exception as exc:
        data.problems.append(f"Could not check recent file activity: {exc}")

    return data


def _recent_activity(settings: Settings) -> str:
    """Files touched recently, as a sign of what he was last working on."""
    from sqlmodel import select

    from app.db import session_scope
    from app.models import IndexedFile

    cutoff = (datetime.now(UTC) - timedelta(hours=ACTIVITY_WINDOW_HOURS)).timestamp()

    # Read the columns *inside* the session. `session_scope` commits on exit,
    # which expires every loaded instance, so touching an attribute afterwards
    # raises DetachedInstanceError rather than returning the value it plainly
    # already had. Copying to tuples here is the whole fix.
    with session_scope(settings) as session:
        recent = [
            (row.path, row.root)
            for row in session.exec(
                select(IndexedFile)
                .where(IndexedFile.mtime >= cutoff)
                .order_by(IndexedFile.mtime.desc())  # type: ignore[union-attr]
                .limit(400)
            ).all()
        ]

    if not recent:
        return ""

    # Group by project folder rather than listing files: "eleven files in
    # ai-secretary-alfred" is the useful shape, not eleven paths.
    from collections import Counter
    from pathlib import Path

    buckets: Counter[str] = Counter()
    for raw_path, raw_root in recent:
        path = Path(raw_path)
        try:
            relative = path.relative_to(raw_root) if raw_root else path
            label = relative.parts[0] if relative.parts else path.name
        except (ValueError, IndexError):
            label = path.parent.name
        buckets[label] += 1

    lines = [f"{count} file(s) in {name}" for name, count in buckets.most_common(6)]
    return f"Changed in the last {ACTIVITY_WINDOW_HOURS} hours:\n" + "\n".join(lines)


BRIEFING_INSTRUCTIONS = """\
Write {user_name}'s morning briefing. You are writing it unprompted, so open
the way a butler opens a door: no greeting about what a briefing is, no
restating the question, straight into what matters.

Rules:
- Lead with anything time-critical today. If nothing is, say so plainly.
- Mention unread mail only if some of it deserves his attention. A count of
  promotional email is not worth his morning.
- One short paragraph, or at most four short lines. He is reading this on a
  phone, half awake.
- If a source could not be read, say which one. Never let an omission read as
  "nothing to report".
- No headings, no bullet symbols, no emoji. Prose, in your voice.
"""


async def compose(settings: Settings | None = None) -> str:
    """Turn the gathered facts into Alfred's briefing."""
    settings = settings or get_settings()
    data = gather(settings)

    if data.is_empty and data.problems:
        # Nothing was readable. Report that honestly rather than asking the
        # model to write a briefing out of nothing, which it will happily do.
        return "I could not prepare a briefing, sir. " + " ".join(data.problems)

    sections = []
    if data.calendar:
        sections.append(wrap_untrusted(data.calendar, "the calendar", settings))
    if data.mail:
        sections.append(wrap_untrusted(data.mail, "the mailbox", settings))
    if data.activity:
        sections.append(f"[recent file activity]\n{data.activity}")
    if data.problems:
        sections.append("[problems]\n" + "\n".join(data.problems))

    instructions = BRIEFING_INSTRUCTIONS.format(user_name=settings.user_name)
    system = build_system_prompt(settings, extra=instructions)

    router = Router(settings)
    chunks: list[str] = []
    try:
        async for delta in router.stream(
            system, [ChatMessage(role="user", content="\n\n".join(sections))]
        ):
            chunks.append(delta)
    except Exception as exc:
        logger.error("Briefing composition failed: %s", exc)
        # Fall back to the raw facts. A plain briefing beats none.
        return _plain(data, settings)

    text = "".join(chunks).strip()
    return text or _plain(data, settings)


def _plain(data: Gathered, settings: Settings) -> str:
    """Unstyled fallback when the model is unreachable."""
    parts = ["Your briefing, sir, unpolished - I could not reach my faculties."]
    if data.calendar:
        parts.append(data.calendar)
    if data.mail:
        parts.append(data.mail)
    if data.activity:
        parts.append(data.activity)
    parts.extend(data.problems)
    return "\n\n".join(parts)


def store(text: str, for_date: str, settings: Settings | None = None) -> int:
    """Save the briefing so a push notification has something to open."""
    from sqlmodel import select

    from app.db import session_scope
    from app.models import Briefing

    settings = settings or get_settings()
    with session_scope(settings) as session:
        existing = session.exec(select(Briefing).where(Briefing.for_date == for_date)).first()
        if existing:
            existing.content = text
            existing.created_at = datetime.now(UTC)
            session.add(existing)
            session.flush()
            assert existing.id is not None
            return existing.id
        row = Briefing(for_date=for_date, content=text)
        session.add(row)
        session.flush()
        assert row.id is not None
        return row.id


def latest(settings: Settings | None = None):
    from sqlmodel import select

    from app.db import session_scope
    from app.models import Briefing

    with session_scope(settings) as session:
        row = session.exec(
            select(Briefing).order_by(Briefing.created_at.desc())  # type: ignore[union-attr]
        ).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "for_date": row.for_date,
            "content": row.content,
            "delivered": row.delivered,
            "created_at": row.created_at.isoformat(),
        }


async def run_and_deliver(settings: Settings | None = None) -> dict:
    """Compose today's briefing, store it, and push it to every device."""
    from app.push import webpush

    settings = settings or get_settings()
    now = _today(settings)
    for_date = now.strftime("%Y-%m-%d")

    text = await compose(settings)
    briefing_id = store(text, for_date, settings)

    # The notification carries only the opening line. The full briefing lives
    # behind the tap, which keeps a locked-screen preview from spilling the
    # day's contents to anyone glancing at the phone.
    preview = text.split("\n")[0][:140]
    delivered, dropped = webpush.send_to_all(
        {
            "title": "Alfred",
            "body": preview,
            "tag": f"alfred-briefing-{for_date}",
            "url": "/?briefing=1",
        },
        settings,
    )

    if delivered:
        from app.db import session_scope
        from app.models import Briefing

        with session_scope(settings) as session:
            row = session.get(Briefing, briefing_id)
            if row:
                row.delivered = True
                session.add(row)

    logger.info("Briefing for %s: delivered to %d device(s), %d dropped.",
                for_date, delivered, dropped)
    return {
        "id": briefing_id,
        "for_date": for_date,
        "content": text,
        "delivered": delivered,
        "dropped": dropped,
    }
