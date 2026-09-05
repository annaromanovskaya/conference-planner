from apscheduler.schedulers.asyncio import AsyncIOScheduler

from conference_scout.scheduler.setup import configure_scheduler


async def noop() -> None:
    return None


def test_scheduler_has_stable_non_overlapping_jobs() -> None:
    scheduler = AsyncIOScheduler()
    configure_scheduler(
        scheduler,
        discovery_job=noop,
        monitoring_job=noop,
        reminder_job=noop,
        discovery_day="sun",
        discovery_hour=10,
        monitoring_hour=9,
        timezone="UTC",
    )
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"weekly-discovery", "daily-monitoring", "hourly-reminders"}
    assert all(job.max_instances == 1 and job.coalesce for job in jobs.values())
