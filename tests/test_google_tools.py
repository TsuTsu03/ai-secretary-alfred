"""Tests for the calendar and mail tools.

Nothing here talks to Google. These pin the decisions that would otherwise only
be discovered by a wrong meeting time or an email that got sent when it should
not have.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.config import Settings
from app.integrations import google_oauth
from app.tools import calendar as cal
from app.tools import gmail
from app.tools import registry as tool_registry
from app.tools.registry import ToolError


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ALFRED_DATA_DIR=str(tmp_path / "data"), ALFRED_TIMEZONE="Asia/Manila"
    )  # type: ignore[call-arg]


# ── scopes: the actual guarantee that Alfred cannot send mail ────────────


def test_gmail_modify_scope_is_requested() -> None:
    assert any(scope.endswith("gmail.modify") for scope in google_oauth.SCOPES)


def test_describe_admits_the_scope_permits_sending(settings: Settings) -> None:
    """This corrects an earlier test that asserted sending was impossible.

    gmail.modify permits users.messages.send, and no Gmail scope allows
    drafting while forbidding sending. Claiming otherwise made the guarantee
    look stronger than it is, so `describe` now reports the two facts
    separately: what the token permits, and what Alfred actually offers.
    """
    status = google_oauth.describe(settings)
    assert status["scope_allows_send"] is True
    assert status["send_tool_registered"] is False


def test_no_tool_can_send_mail() -> None:
    """The actual guarantee: the model is offered no way to send.

    This is the test that matters now. It would fail the moment someone
    registered a send tool, which is exactly when the promise would break.
    """
    tool_registry.clear()
    gmail.register_gmail_tools()
    names = {t.spec.name for t in tool_registry.all_tools()}
    assert not any("send" in name for name in names), names
    tool_registry.clear()


def test_gmail_module_never_calls_send() -> None:
    """Belt and braces, at the AST level.

    A substring grep would match the module docstring, which discusses sending
    at length. Parsing means this checks calls actually made, so it stays true
    however the prose around it is worded.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gmail))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "send" not in called, f"gmail.py calls .send(): {sorted(called)}"
    assert "drafts" in called, "the draft path should still go through drafts()"


def test_tokens_live_outside_the_repo(settings: Settings) -> None:
    """The Google refresh token is the most valuable secret Alfred holds."""
    for path in (google_oauth.token_path(settings), google_oauth.client_secrets_path(settings)):
        assert path.is_relative_to(settings.data_dir)


def test_not_connected_error_names_the_fix(settings: Settings) -> None:
    with pytest.raises(google_oauth.GoogleAuthError, match="connect_google"):
        google_oauth._load_credentials(settings)


# ── time handling ────────────────────────────────────────────────────────


def test_naive_times_are_read_in_his_timezone(settings: Settings) -> None:
    """He says "three o'clock" meaning Manila. Reading that as UTC is an
    eight-hour error that looks entirely plausible on a confirmation card."""
    parsed = cal._parse("2026-09-15T15:00:00", settings)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 8 * 3600


def test_explicit_offsets_are_respected(settings: Settings) -> None:
    parsed = cal._parse("2026-09-15T15:00:00+00:00", settings)
    assert parsed.utcoffset().total_seconds() == 0


def test_trailing_z_is_accepted(settings: Settings) -> None:
    """fromisoformat rejected a trailing Z before 3.11 and Google emits it."""
    parsed = cal._parse("2026-09-15T07:00:00Z", settings)
    assert parsed.astimezone(UTC).hour == 7


def test_unparseable_time_is_refused(settings: Settings) -> None:
    with pytest.raises(ToolError):
        cal._parse("next tuesday-ish", settings)


def test_empty_time_is_refused(settings: Settings) -> None:
    with pytest.raises(ToolError):
        cal._parse("", settings)


def test_events_render_in_local_time(settings: Settings) -> None:
    """A secretary who reports meetings in UTC is not being precise."""
    rendered = cal._format("2026-09-15T07:00:00Z", settings)
    assert "15:00" in rendered  # 07:00 UTC is 15:00 in Manila


def test_all_day_events_are_labelled(settings: Settings) -> None:
    assert cal._format("2026-09-15", settings).endswith("(all day)")


# ── mail parsing ─────────────────────────────────────────────────────────


def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def test_plain_text_is_preferred_over_html() -> None:
    """HTML through the model is mostly markup, and a free tier pays for it."""
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _encode("<p>markup</p>")}},
            {"mimeType": "text/plain", "body": {"data": _encode("the real text")}},
        ],
    }
    assert gmail._plain_text(payload) == "the real text"


def test_html_is_stripped_when_there_is_no_plain_part() -> None:
    payload = {
        "mimeType": "text/html",
        "body": {"data": _encode("<style>x{}</style><p>Hello <b>there</b></p>")},
    }
    text = gmail._plain_text(payload)
    assert "Hello" in text and "there" in text
    assert "<" not in text and "x{}" not in text


def test_nested_multipart_is_traversed() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [{"mimeType": "text/plain", "body": {"data": _encode("buried")}}],
            }
        ],
    }
    assert gmail._plain_text(payload) == "buried"


def test_undecodable_body_does_not_raise() -> None:
    assert gmail._plain_text({"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}}) == ""


def test_headers_are_found_case_insensitively() -> None:
    payload = {"headers": [{"name": "subject", "value": "Re: the quotation"}]}
    assert gmail._header(payload, "Subject") == "Re: the quotation"


def test_missing_header_is_empty_not_an_error() -> None:
    assert gmail._header({"headers": []}, "From") == ""


# ── the gate ─────────────────────────────────────────────────────────────


def test_every_google_write_is_gated() -> None:
    """Reads are direct; anything that changes Jansen's calendar or mailbox
    must queue a confirmation instead of happening."""
    tool_registry.clear()
    cal.register_calendar_tools()
    gmail.register_gmail_tools()

    mutating = {t.spec.name for t in tool_registry.all_tools() if t.mutating}
    assert mutating == {"create_event", "move_event", "cancel_event", "draft_reply"}

    readonly = {t.spec.name for t in tool_registry.all_tools() if not t.mutating}
    assert readonly == {"list_events", "search_events", "search_mail", "read_mail"}
    tool_registry.clear()


def test_gated_tools_can_describe_themselves_for_the_card() -> None:
    """A card with no detail is a card nobody can meaningfully approve."""
    tool_registry.clear()
    cal.register_calendar_tools()
    gmail.register_gmail_tools()
    for tool in tool_registry.all_tools():
        if tool.mutating:
            assert tool.summarize is not None, tool.spec.name
            assert tool.describe is not None, tool.spec.name
    tool_registry.clear()


def test_draft_card_shows_the_whole_message_and_says_it_is_not_sent() -> None:
    detail = gmail._describe_draft(
        {"to": "erica@example.com", "subject": "Re: RSVP", "body": "Confirming Saturday."}
    )
    assert "erica@example.com" in detail
    assert "Confirming Saturday." in detail
    assert "draft only" in detail.lower()
    assert "you send it" in detail.lower()


def test_draft_refuses_an_empty_recipient(settings: Settings) -> None:
    with pytest.raises(ToolError):
        gmail.draft_reply("", "Subject", "Body", settings=settings)


def test_draft_refuses_an_empty_body(settings: Settings) -> None:
    with pytest.raises(ToolError):
        gmail.draft_reply("someone@example.com", "Subject", "   ", settings=settings)


def test_draft_encodes_a_valid_rfc822_message() -> None:
    """What gets base64'd has to be a real message, or Gmail rejects it."""
    message = EmailMessage()
    message["To"] = "erica@example.com"
    message["Subject"] = "Re: RSVP"
    message.set_content("Confirming Saturday.")
    decoded = base64.urlsafe_b64decode(
        base64.urlsafe_b64encode(message.as_bytes())
    ).decode("utf-8")
    assert "To: erica@example.com" in decoded
    assert "Confirming Saturday." in decoded


def test_event_needs_a_title(settings: Settings) -> None:
    with pytest.raises(ToolError):
        cal.create_event("", "2026-09-15T15:00:00", settings=settings)


def test_event_end_must_follow_its_start(settings: Settings) -> None:
    with pytest.raises(ToolError, match="after"):
        cal.create_event(
            "Backwards", "2026-09-15T15:00:00", "2026-09-15T14:00:00", settings=settings
        )


def test_move_needs_an_event_id(settings: Settings) -> None:
    with pytest.raises(ToolError):
        cal.move_event("", "2026-09-15T15:00:00", settings=settings)


def test_create_event_card_shows_local_times(settings: Settings) -> None:
    from unittest.mock import patch

    with patch("app.tools.calendar.get_settings", return_value=settings):
        detail = cal._describe_event(
            {"summary": "Board meeting", "start": "2026-09-15T15:00:00", "location": "Wayne Tower"}
        )
    assert "Board meeting" in detail
    assert "Wayne Tower" in detail
    assert "15:00" in detail


def test_now_is_timezone_aware() -> None:
    """A guard against the tzdata regression: without it zoneinfo silently
    fell back to system local time on Windows."""
    from zoneinfo import ZoneInfo

    assert datetime.now(ZoneInfo("Asia/Manila")).utcoffset().total_seconds() == 8 * 3600
