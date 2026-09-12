"""Tests for the daily briefing and push.

Nothing here contacts a push service. These pin the decisions that make the
difference between a briefing that is useful and one that quietly lies by
omission.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import briefing
from app.config import Settings
from app.jobs import scheduler
from app.push import webpush


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ALFRED_DATA_DIR=str(tmp_path / "data"), ALFRED_TIMEZONE="Asia/Manila"
    )  # type: ignore[call-arg]


# ── VAPID ────────────────────────────────────────────────────────────────


def test_vapid_keys_are_generated_once_and_reused(settings: Settings) -> None:
    """Regenerating would silently invalidate every existing subscription."""
    first = webpush.ensure_keys(settings)
    second = webpush.ensure_keys(settings)
    assert first.public_key == second.public_key


def test_public_key_is_a_raw_p256_point(settings: Settings) -> None:
    """The browser wants the uncompressed point, base64url, unpadded: 65 bytes
    becomes 87 characters. A PEM or a DER blob here fails at subscribe time
    with an opaque error."""
    import base64

    key = webpush.public_key(settings)
    assert len(key) == 87
    assert "=" not in key and "+" not in key and "/" not in key
    raw = base64.urlsafe_b64decode(key + "==")
    assert len(raw) == 65
    assert raw[0] == 0x04  # uncompressed point marker


def test_keys_live_outside_the_repo(settings: Settings) -> None:
    webpush.ensure_keys(settings)
    assert webpush._keys_path(settings).is_relative_to(settings.data_dir)


def test_send_to_all_with_no_subscribers_is_not_an_error(settings: Settings) -> None:
    from app.db import init_db

    init_db(settings)
    assert webpush.send_to_all({"title": "Alfred", "body": "x"}, settings) == (0, 0)


def test_describe_states_the_ios_requirement(settings: Settings) -> None:
    """The single most common reason a briefing never arrives on a phone."""
    from app.db import init_db

    init_db(settings)
    assert webpush.describe(settings)["ios_requires_https_pwa"] is True


# ── gathering ────────────────────────────────────────────────────────────


def test_gather_reports_a_missing_google_rather_than_staying_silent(
    settings: Settings,
) -> None:
    """An omission that reads as "nothing to report" is a lie by layout."""
    data = briefing.gather(settings)
    assert any("Google is not connected" in p for p in data.problems)


def test_gather_never_raises(settings: Settings) -> None:
    """A briefing that crashes at 07:00 is a briefing nobody ever sees."""
    data = briefing.gather(settings)
    assert isinstance(data.problems, list)
    assert data.for_date


def test_empty_gather_with_problems_is_reported_honestly(settings: Settings) -> None:
    """Rather than asking the model to write a briefing out of nothing, which
    it will cheerfully do."""
    import asyncio

    text = asyncio.run(briefing.compose(settings))
    assert "could not prepare a briefing" in text.lower()


# ── storage ──────────────────────────────────────────────────────────────


def test_storing_twice_on_one_day_updates_rather_than_duplicates(
    settings: Settings,
) -> None:
    """Two runs on the same day should leave one briefing, not two."""
    from app.db import init_db

    init_db(settings)
    first = briefing.store("morning one", "2026-09-12", settings)
    second = briefing.store("morning two", "2026-09-12", settings)
    assert first == second
    assert briefing.latest(settings)["content"] == "morning two"


def test_latest_is_none_before_any_briefing(settings: Settings) -> None:
    from app.db import init_db

    init_db(settings)
    assert briefing.latest(settings) is None


def test_recent_activity_survives_the_session_closing(settings: Settings) -> None:
    """Regression: reading columns after session_scope exits raises
    DetachedInstanceError, which surfaced in a real briefing as "unable to
    inspect your recent file activity due to a database error"."""
    from app.db import init_db, session_scope
    from app.models import IndexedFile

    init_db(settings)
    with session_scope(settings) as session:
        session.add(
            IndexedFile(
                path=str(Path("C:/CodingProjects/demo/notes.md")),
                root="C:/CodingProjects",
                mtime=(datetime.now(UTC) - timedelta(hours=2)).timestamp(),
            )
        )

    summary = briefing._recent_activity(settings)
    assert "demo" in summary


def test_recent_activity_ignores_stale_files(settings: Settings) -> None:
    from app.db import init_db, session_scope
    from app.models import IndexedFile

    init_db(settings)
    with session_scope(settings) as session:
        session.add(
            IndexedFile(
                path=str(Path("C:/CodingProjects/old/ancient.md")),
                root="C:/CodingProjects",
                mtime=(datetime.now(UTC) - timedelta(days=30)).timestamp(),
            )
        )
    assert briefing._recent_activity(settings) == ""


# ── scheduling ───────────────────────────────────────────────────────────


def test_briefing_is_enabled_by_default() -> None:
    """A secretary who has to be asked to start the day is not much of one."""
    assert Settings(ALFRED_DATA_DIR="x").briefing_enabled is True  # type: ignore[call-arg]


def test_misfire_grace_covers_a_laptop_that_slept(settings: Settings) -> None:
    """A closed laptop routinely misses 07:00 and opens at 09:14. Without a
    grace window APScheduler drops the run and no briefing ever arrives."""
    assert scheduler.MISFIRE_GRACE_SECONDS >= 2 * 60 * 60


def test_describe_reports_the_schedule_even_when_stopped(settings: Settings) -> None:
    status = scheduler.describe(settings)
    assert status["at"] == "07:00"
    assert status["enabled"] is True


def test_disabled_briefing_does_not_start(tmp_path: Path) -> None:
    off = Settings(
        ALFRED_DATA_DIR=str(tmp_path / "d"), ALFRED_BRIEFING_ENABLED=False
    )  # type: ignore[call-arg]
    assert scheduler.start(off) is None
