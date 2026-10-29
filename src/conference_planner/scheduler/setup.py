from collections.abc import Callable, Coroutine
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

AsyncJob = Callable[[], Coroutine[Any, Any, None]]


def configure_scheduler(
    scheduler: AsyncIOScheduler,
    *,
    conference_cycle_job: AsyncJob,
    reminder_job: AsyncJob,
    test_reminder_job: AsyncJob,
    discovery_hour: int,
    discovery_interval_hours: int = 168,
    reminder_hour: int,
    timezone: str,
) -> None:
    if discovery_interval_hours < 1:
        raise ValueError("discovery_interval_hours must be positive")
    common = {"replace_existing": True, "coalesce": True, "max_instances": 1}
    scheduler.add_job(
        conference_cycle_job,
        "interval",
        id="periodic-conference-cycle",
        hours=discovery_interval_hours,
        **common,
    )
    scheduler.add_job(
        reminder_job,
        "cron",
        id="daily-deadline-reminders",
        hour=reminder_hour,
        timezone=timezone,
        **common,
    )
    scheduler.add_job(
        test_reminder_job,
        "interval",
        id="test-reminders",
        minutes=1,
        **common,
    )
