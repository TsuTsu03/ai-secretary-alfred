"""The scheduler that wakes Alfred up before Jansen does.

One job: compose and deliver the morning briefing. APScheduler rather than a
thread and a sleep loop, because a cron trigger understands timezones, and
"07:00 in Manila" has to survive the machine being asleep at 07:00.

``misfire_grace_time`` matters more than it looks. A laptop is closed overnight,
so the 07:00 trigger routinely fires late - at 09:14, when the lid opens.
Without a grace window APScheduler drops the run entirely and no briefing ever
arrives on a laptop that sleeps, which is every laptop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_scheduler = None

JOB_ID = "alfred-morning-briefing"

# Four hours. Long enough to catch a laptop opened mid-morning, short enough
# that yesterday's briefing never arrives today.
MISFIRE_GRACE_SECONDS = 4 * 60 * 60


def _run_briefing() -> None:
    """Bridge the sync scheduler to the async briefing."""
    from app import briefing

    try:
        asyncio.run(briefing.run_and_deliver())
    except Exception:
        logger.exception("The morning briefing failed")


def start(settings: Settings | None = None):
    """Start the scheduler. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = settings or get_settings()
    if not settings.briefing_enabled:
        logger.info("Daily briefing is disabled (ALFRED_BRIEFING_ENABLED=false).")
        return None

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    try:
        trigger = CronTrigger(
            hour=settings.briefing_hour,
            minute=settings.briefing_minute,
            timezone=settings.timezone,
        )
    except Exception as exc:
        logger.error("Could not schedule the briefing (%s); it is disabled.", exc)
        return None

    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        _run_briefing,
        trigger=trigger,
        id=JOB_ID,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        coalesce=True,  # one briefing after a long sleep, not a queue of them
        max_instances=1,
    )
    scheduler.start()
    _scheduler = scheduler

    logger.info(
        "Morning briefing scheduled for %02d:%02d %s.",
        settings.briefing_hour, settings.briefing_minute, settings.timezone,
    )
    return scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def describe(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    if _scheduler is None:
        return {
            "running": False,
            "enabled": settings.briefing_enabled,
            "at": f"{settings.briefing_hour:02d}:{settings.briefing_minute:02d}",
        }
    job = _scheduler.get_job(JOB_ID)
    next_run = getattr(job, "next_run_time", None) if job else None
    return {
        "running": True,
        "enabled": True,
        "at": f"{settings.briefing_hour:02d}:{settings.briefing_minute:02d}",
        "timezone": settings.timezone,
        "next_run": next_run.isoformat() if next_run else "",
        "now": datetime.now(UTC).isoformat(),
    }
