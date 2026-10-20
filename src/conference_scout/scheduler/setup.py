from collections.abc import Callable, Coroutine
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

AsyncJob = Callable[[], Coroutine[Any, Any, None]]


def configure_scheduler(
    scheduler: AsyncIOScheduler,
    *,
    conference_cycle_job: AsyncJob,
    reminder_job: AsyncJob,
    discovery_hour: int,
    reminder_hour: int,
    timezone: str,
) -> None:
    common = {"replace_existing": True, "coalesce": True, "max_instances": 1}
    scheduler.add_job(
        conference_cycle_job,
        "cron",
        id="weekly-conference-cycle",
        day_of_week="mon",
        hour=discovery_hour,
        timezone=timezone,
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
