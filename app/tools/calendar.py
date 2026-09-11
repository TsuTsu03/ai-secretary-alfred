"""Calendar tools.

Reads are direct. Anything that changes the calendar - creating, moving, or
cancelling - is registered as mutating, so a model tool call queues a
confirmation card and the change only happens once Jansen approves it.

Times are rendered in his configured timezone rather than UTC. A secretary who
reports meetings in UTC is not being precise, he is being useless.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings, get_settings
from app.integrations import google_oauth
from app.llm.base import ToolSpec
from app.tools.registry import Tool, ToolError, register

logger = logging.getLogger(__name__)

MAX_RESULTS = 25


def _zone(settings: Settings):
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r; using UTC.", settings.timezone)
        return UTC


def _parse(value: str, settings: Settings) -> datetime:
    """Accept an ISO timestamp, with or without a zone, and return an aware one.

    A naive timestamp is interpreted in Jansen's timezone, not UTC. He says
    "three o'clock" meaning Manila, and a model echoing that back as naive ISO
    must not silently become 3am UTC - an eight-hour error that looks plausible
    on the confirmation card.
    """
    raw = (value or "").strip()
    if not raw:
        raise ToolError("a time is required")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ToolError(f"{value!r} is not a time I can read (use ISO 8601)") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_zone(settings))
    return parsed


def _format(value: str, settings: Settings) -> str:
    """Render an event time in local terms."""
    if not value:
        return "(no time)"
    # All-day events carry a bare date.
    if len(value) == 10:
        return value + " (all day)"
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return moment.astimezone(_zone(settings)).strftime("%a %d %b, %H:%M")


def _calendar(settings: Settings):
    try:
        return google_oauth.service("calendar", "v3", settings)
    except google_oauth.GoogleAuthError as exc:
        raise ToolError(str(exc)) from exc


def _event_line(event: dict, settings: Settings) -> str:
    start = event.get("start", {})
    when = _format(start.get("dateTime") or start.get("date", ""), settings)
    summary = event.get("summary") or "(no title)"
    location = event.get("location")
    attendees = event.get("attendees") or []
    parts = [f"{when} — {summary}"]
    if location:
        parts.append(f"at {location}")
    if attendees:
        names = [a.get("email", "") for a in attendees[:4] if a.get("email")]
        if names:
            parts.append(f"with {', '.join(names)}")
    parts.append(f"[id {event.get('id', '')}]")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def list_events(days: int = 1, settings: Settings | None = None) -> str:
    """Upcoming events over the next `days` days."""
    settings = settings or get_settings()
    span = max(1, min(int(days or 1), 60))

    now = datetime.now(UTC)
    try:
        response = (
            _calendar(settings)
            .events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=(now + timedelta(days=span)).isoformat(),
                singleEvents=True,  # expand recurrences into real occurrences
                orderBy="startTime",
                maxResults=MAX_RESULTS,
            )
            .execute()
        )
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"could not read the calendar: {exc}") from exc

    events = response.get("items", [])
    if not events:
        return f"Nothing in the calendar for the next {span} day(s)."
    header = f"{len(events)} event(s) in the next {span} day(s), {settings.timezone}:"
    return header + "\n" + "\n".join(_event_line(e, settings) for e in events)


def search_events(query: str, settings: Settings | None = None) -> str:
    """Find events by text, past or future."""
    settings = settings or get_settings()
    if not (query or "").strip():
        raise ToolError("a search needs a query")

    try:
        response = (
            _calendar(settings)
            .events()
            .list(
                calendarId="primary",
                q=query,
                singleEvents=True,
                orderBy="startTime",
                maxResults=MAX_RESULTS,
                timeMin=(datetime.now(UTC) - timedelta(days=180)).isoformat(),
            )
            .execute()
        )
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"could not search the calendar: {exc}") from exc

    events = response.get("items", [])
    if not events:
        return f"No events match {query!r}."
    return "\n".join(_event_line(e, settings) for e in events)


# ---------------------------------------------------------------------------
# writes - gated
# ---------------------------------------------------------------------------


def create_event(
    summary: str,
    start: str,
    end: str = "",
    location: str = "",
    description: str = "",
    settings: Settings | None = None,
) -> str:
    """Create an event. Only ever reached via an approved PendingAction."""
    settings = settings or get_settings()
    if not (summary or "").strip():
        raise ToolError("an event needs a title")

    begins = _parse(start, settings)
    # A meeting with no stated end is an hour. Better a sane default than an
    # event Google rejects.
    ends = _parse(end, settings) if end else begins + timedelta(hours=1)
    if ends <= begins:
        raise ToolError("the end time must be after the start time")

    body = {
        "summary": summary.strip(),
        "start": {"dateTime": begins.isoformat(), "timeZone": settings.timezone},
        "end": {"dateTime": ends.isoformat(), "timeZone": settings.timezone},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description

    try:
        created = _calendar(settings).events().insert(calendarId="primary", body=body).execute()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"could not create the event: {exc}") from exc

    return f"Created '{created.get('summary')}' on {_format(begins.isoformat(), settings)}."


def move_event(event_id: str, start: str, end: str = "", settings: Settings | None = None) -> str:
    """Reschedule an event. Only ever reached via an approved PendingAction."""
    settings = settings or get_settings()
    if not (event_id or "").strip():
        raise ToolError("which event? an event id is required")

    service = _calendar(settings)
    try:
        existing = service.events().get(calendarId="primary", eventId=event_id).execute()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"could not find event {event_id!r}: {exc}") from exc

    begins = _parse(start, settings)
    if end:
        ends = _parse(end, settings)
    else:
        # Preserve the original duration rather than assuming an hour: moving a
        # thirty-minute call should not quietly make it an hour long.
        old_start = existing.get("start", {}).get("dateTime")
        old_end = existing.get("end", {}).get("dateTime")
        duration = timedelta(hours=1)
        if old_start and old_end:
            try:
                duration = datetime.fromisoformat(
                    old_end.replace("Z", "+00:00")
                ) - datetime.fromisoformat(old_start.replace("Z", "+00:00"))
            except ValueError:
                pass
        ends = begins + duration

    existing["start"] = {"dateTime": begins.isoformat(), "timeZone": settings.timezone}
    existing["end"] = {"dateTime": ends.isoformat(), "timeZone": settings.timezone}

    try:
        updated = (
            service.events()
            .update(calendarId="primary", eventId=event_id, body=existing)
            .execute()
        )
    except Exception as exc:
        raise ToolError(f"could not move the event: {exc}") from exc

    return f"Moved '{updated.get('summary')}' to {_format(begins.isoformat(), settings)}."


def cancel_event(event_id: str, settings: Settings | None = None) -> str:
    """Delete an event. Only ever reached via an approved PendingAction."""
    settings = settings or get_settings()
    if not (event_id or "").strip():
        raise ToolError("which event? an event id is required")
    try:
        service = _calendar(settings)
        existing = service.events().get(calendarId="primary", eventId=event_id).execute()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"could not cancel the event: {exc}") from exc
    return f"Cancelled '{existing.get('summary')}'."


# ---------------------------------------------------------------------------
# confirmation cards
# ---------------------------------------------------------------------------


def _describe_event(arguments: dict) -> str:
    settings = get_settings()
    lines = [f"Title:    {arguments.get('summary', '(none)')}"]
    if arguments.get("start"):
        lines.append(f"Starts:   {_format(_parse(arguments['start'], settings).isoformat(), settings)}")
    if arguments.get("end"):
        lines.append(f"Ends:     {_format(_parse(arguments['end'], settings).isoformat(), settings)}")
    if arguments.get("location"):
        lines.append(f"Location: {arguments['location']}")
    if arguments.get("description"):
        lines.append(f"\n{arguments['description']}")
    return "\n".join(lines)


def _describe_move(arguments: dict) -> str:
    settings = get_settings()
    lines = [f"Event id: {arguments.get('event_id', '(none)')}"]
    try:
        existing = (
            _calendar(settings)
            .events()
            .get(calendarId="primary", eventId=arguments.get("event_id", ""))
            .execute()
        )
        start = existing.get("start", {})
        lines.insert(0, f"Event:    {existing.get('summary', '(no title)')}")
        lines.append(f"Was:      {_format(start.get('dateTime') or start.get('date', ''), settings)}")
    except Exception:
        # The card is still useful without the current time; do not fail the
        # whole confirmation because a lookup did not come back.
        pass
    if arguments.get("start"):
        lines.append(f"Moves to: {_format(_parse(arguments['start'], settings).isoformat(), settings)}")
    return "\n".join(lines)


def _describe_cancel(arguments: dict) -> str:
    settings = get_settings()
    try:
        existing = (
            _calendar(settings)
            .events()
            .get(calendarId="primary", eventId=arguments.get("event_id", ""))
            .execute()
        )
        start = existing.get("start", {})
        return (
            f"Cancel: {existing.get('summary', '(no title)')}\n"
            f"When:   {_format(start.get('dateTime') or start.get('date', ''), settings)}\n"
            f"Id:     {arguments.get('event_id')}\n\n"
            f"This deletes the event and notifies any attendees."
        )
    except Exception:
        return f"Cancel event {arguments.get('event_id')}. This cannot be undone."


def register_calendar_tools() -> None:
    register(
        Tool(
            spec=ToolSpec(
                name="list_events",
                description=(
                    "Read Jansen's upcoming calendar events. Use for any question about "
                    "his schedule, what is on today, or when he is free."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "How many days ahead to look (1-60). Default 1.",
                        }
                    },
                    "required": [],
                },
            ),
            run=lambda settings, days=1: list_events(days, settings),
        )
    )

    register(
        Tool(
            spec=ToolSpec(
                name="search_events",
                description=(
                    "Find calendar events by text - a person's name, a project, a place. "
                    "Searches the last six months and everything upcoming."
                ),
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Text to match."}},
                    "required": ["query"],
                },
            ),
            run=lambda settings, query: search_events(query, settings),
        )
    )

    register(
        Tool(
            spec=ToolSpec(
                name="create_event",
                description=(
                    "Propose a new calendar event. This does NOT create it - it puts a "
                    "confirmation card in front of Jansen and he decides. Never tell him "
                    "an event exists from this call alone."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "Event title."},
                        "start": {
                            "type": "string",
                            "description": "ISO 8601 start, e.g. 2026-09-15T14:00:00. "
                            "A time without a zone is read in his local timezone.",
                        },
                        "end": {"type": "string", "description": "ISO 8601 end. Defaults to an hour."},
                        "location": {"type": "string", "description": "Where."},
                        "description": {"type": "string", "description": "Notes."},
                    },
                    "required": ["summary", "start"],
                },
            ),
            run=lambda settings, summary, start, end="", location="", description="": create_event(
                summary, start, end, location, description, settings
            ),
            mutating=True,
            summarize=lambda a: f"Add '{a.get('summary', '?')}' to the calendar",
            describe=_describe_event,
        )
    )

    register(
        Tool(
            spec=ToolSpec(
                name="move_event",
                description=(
                    "Propose rescheduling an event. Requires the event id from "
                    "list_events or search_events. Queues a confirmation; does not move it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "Id from a listing."},
                        "start": {"type": "string", "description": "New ISO 8601 start."},
                        "end": {
                            "type": "string",
                            "description": "New ISO 8601 end. Omit to keep the same duration.",
                        },
                    },
                    "required": ["event_id", "start"],
                },
            ),
            run=lambda settings, event_id, start, end="": move_event(
                event_id, start, end, settings
            ),
            mutating=True,
            summarize=lambda a: f"Reschedule event {a.get('event_id', '?')}",
            describe=_describe_move,
        )
    )

    register(
        Tool(
            spec=ToolSpec(
                name="cancel_event",
                description=(
                    "Propose cancelling an event. Queues a confirmation; does not cancel it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "Id from a listing."}
                    },
                    "required": ["event_id"],
                },
            ),
            run=lambda settings, event_id: cancel_event(event_id, settings),
            mutating=True,
            summarize=lambda a: f"Cancel event {a.get('event_id', '?')}",
            describe=_describe_cancel,
        )
    )
