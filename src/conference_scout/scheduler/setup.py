from collections.abc import Callable, Coroutine
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

AsyncJob = Callable[[], Coroutine[Any, Any, None]]


def configure_scheduler(
    scheduler: AsyncIOScheduler,
    *,
    discovery_job: AsyncJob,
    monitoring_job: AsyncJob,
    reminder_job: AsyncJob,
    discovery_day: str,
    discovery_hour: int,
    monitoring_hour: int,
    timezone: str,
) -> None:
    common = {"replace_existing": True, "coalesce": True, "max_instances": 1}
    scheduler.add_job(
        discovery_job,
        "cron",
        id="weekly-discovery",
        day_of_week=discovery_day,
        hour=discovery_hour,
        timezone=timezone,
        **common,
    )
    scheduler.add_job(
        monitoring_job,
        "cron",
        id="daily-monitoring",
        hour=monitoring_hour,
        timezone=timezone,
        **common,
    )
    scheduler.add_job(
        reminder_job,
        "cron",
        id="hourly-reminders",
        minute=0,
        timezone=timezone,
        **common,
    )
