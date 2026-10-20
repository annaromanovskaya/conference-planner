from apscheduler.schedulers.asyncio import AsyncIOScheduler

from conference_scout.scheduler.setup import configure_scheduler


async def noop() -> None:
    return None


def test_scheduler_has_stable_non_overlapping_jobs() -> None:
    scheduler = AsyncIOScheduler()
    configure_scheduler(
        scheduler,
        conference_cycle_job=noop,
        reminder_job=noop,
        discovery_hour=10,
        reminder_hour=9,
        timezone="UTC",
    )
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"weekly-conference-cycle", "daily-deadline-reminders"}
    assert all(job.max_instances == 1 and job.coalesce for job in jobs.values())
    assert "day_of_week='mon'" in str(jobs["weekly-conference-cycle"].trigger)
    assert "hour='10'" in str(jobs["weekly-conference-cycle"].trigger)
    assert "hour='9'" in str(jobs["daily-deadline-reminders"].trigger)
