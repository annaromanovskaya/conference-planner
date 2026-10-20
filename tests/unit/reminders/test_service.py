from datetime import date

import pytest

from conference_scout.reminders.service import (
    NotificationKind,
    ReminderCandidate,
    ReminderService,
    due_offset,
)


def test_due_offset_uses_calendar_days() -> None:
    assert due_offset(date(2027, 2, 12), date(2027, 1, 29), (30, 14, 7)) == 14


def test_due_offset_ignores_unconfigured_and_past_dates() -> None:
    assert due_offset(date(2027, 2, 12), date(2027, 1, 30), (14, 7)) is None
    assert due_offset(date(2027, 2, 12), date(2027, 2, 13), (14, 7)) is None


@pytest.mark.parametrize("days", [(), (-1, 7)])
def test_due_offset_rejects_invalid_configuration(days: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        due_offset(date(2027, 2, 12), date(2027, 2, 1), days)


class FakeReminderRepository:
    def __init__(self, candidates=None) -> None:
        self.values = candidates or [
            ReminderCandidate(7, "Astro", "ABSTRACT_SUBMISSION", date(2027, 2, 12))
        ]
        self.recorded: list[tuple[int, int, str, int]] = []

    async def candidates(self, _user_id: int) -> list[ReminderCandidate]:
        return self.values

    async def was_sent(self, user_id, deadline_id, notification_kind, threshold) -> bool:
        return (user_id, deadline_id, notification_kind, threshold) in self.recorded

    async def record(self, user_id, deadline_id, notification_kind, threshold) -> None:
        self.recorded.append((user_id, deadline_id, notification_kind, threshold))


@pytest.mark.asyncio
async def test_failed_send_is_not_recorded() -> None:
    repository = FakeReminderRepository()

    async def failing_sender(_notice) -> None:
        raise RuntimeError("telegram unavailable")

    report = await ReminderService(repository, (14,)).process_daily(
        1, date(2027, 1, 29), failing_sender
    )
    assert repository.recorded == []
    assert report.failed == 1


@pytest.mark.asyncio
async def test_sent_reminder_is_not_repeated() -> None:
    repository = FakeReminderRepository()
    sent = 0

    async def sender(_notice) -> None:
        nonlocal sent
        sent += 1

    service = ReminderService(repository, (14,))
    await service.process_daily(1, date(2027, 1, 29), sender)
    await service.process_daily(1, date(2027, 1, 29), sender)
    assert sent == 1


@pytest.mark.asyncio
async def test_after_downtime_only_latest_crossed_threshold_is_sent() -> None:
    repository = FakeReminderRepository()
    notices = []
    service = ReminderService(repository, (30, 14, 7, 3, 1, 0))

    async def sender(notice) -> None:
        notices.append(notice)

    await service.process_daily(1, date(2027, 1, 31), sender)
    await service.process_daily(1, date(2027, 2, 1), sender)

    assert len(notices) == 1
    assert notices[0].notification_kind == NotificationKind.EXACT
    assert notices[0].threshold == 14
    assert notices[0].days_remaining == 12


@pytest.mark.asyncio
async def test_exact_deadline_day_and_approximate_month_start_are_supported() -> None:
    repository = FakeReminderRepository(
        [
            ReminderCandidate(7, "Astro", "PAYMENT", date(2027, 5, 1)),
            ReminderCandidate(
                8,
                "IMBASE",
                "REGISTRATION",
                deadline_date=None,
                approximate_year=2027,
                approximate_month=5,
            ),
        ]
    )
    notices = []

    async def sender(notice) -> None:
        notices.append(notice)

    await ReminderService(repository, (30, 14, 7, 3, 1, 0)).process_daily(
        1, date(2027, 5, 1), sender
    )

    assert [item.notification_kind for item in notices] == [
        NotificationKind.EXACT,
        NotificationKind.APPROXIMATE_MONTH_START,
    ]
    assert notices[0].threshold == 0
    assert notices[1].threshold == 0
