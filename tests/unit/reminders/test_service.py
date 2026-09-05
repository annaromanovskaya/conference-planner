from datetime import date

import pytest

from conference_scout.reminders.service import ReminderCandidate, ReminderService, due_offset


def test_due_offset_uses_calendar_days() -> None:
    assert due_offset(date(2027, 2, 12), date(2027, 1, 29), (30, 14, 7)) == 14


def test_due_offset_ignores_unconfigured_and_past_dates() -> None:
    assert due_offset(date(2027, 2, 12), date(2027, 1, 30), (14, 7)) is None
    assert due_offset(date(2027, 2, 12), date(2027, 2, 13), (14, 7)) is None


@pytest.mark.parametrize("days", [(), (0,), (-1, 7)])
def test_due_offset_rejects_invalid_configuration(days: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        due_offset(date(2027, 2, 12), date(2027, 2, 1), days)


class FakeReminderRepository:
    def __init__(self) -> None:
        self.recorded: list[tuple[int, int, int]] = []

    async def candidates(self, _user_id: int) -> list[ReminderCandidate]:
        return [ReminderCandidate(7, "Astro", "ABSTRACT_SUBMISSION", date(2027, 2, 12))]

    async def was_sent(self, user_id: int, deadline_id: int, days: int) -> bool:
        return (user_id, deadline_id, days) in self.recorded

    async def record(self, user_id: int, deadline_id: int, days: int) -> None:
        self.recorded.append((user_id, deadline_id, days))


@pytest.mark.asyncio
async def test_failed_send_is_not_recorded() -> None:
    repository = FakeReminderRepository()

    async def failing_sender(_candidate: ReminderCandidate, _days: int) -> None:
        raise RuntimeError("telegram unavailable")

    report = await ReminderService(repository, (14,)).process(1, date(2027, 1, 29), failing_sender)
    assert repository.recorded == []
    assert report.failed == 1


@pytest.mark.asyncio
async def test_sent_reminder_is_not_repeated() -> None:
    repository = FakeReminderRepository()
    sent = 0

    async def sender(_candidate: ReminderCandidate, _days: int) -> None:
        nonlocal sent
        sent += 1

    service = ReminderService(repository, (14,))
    await service.process(1, date(2027, 1, 29), sender)
    await service.process(1, date(2027, 1, 29), sender)
    assert sent == 1
