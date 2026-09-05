from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ReminderCandidate:
    deadline_id: int
    conference_name: str
    deadline_type: str
    deadline_date: date


class ReminderStore(Protocol):
    async def candidates(self, user_id: int) -> list[ReminderCandidate]: ...

    async def was_sent(self, user_id: int, deadline_id: int, days: int) -> bool: ...

    async def record(self, user_id: int, deadline_id: int, days: int) -> None: ...


ReminderSender = Callable[[ReminderCandidate, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReminderReport:
    sent: int = 0
    failed: int = 0


def due_offset(deadline: date, today: date, offsets: tuple[int, ...]) -> int | None:
    if not offsets or any(offset <= 0 for offset in offsets):
        raise ValueError("reminder offsets must be positive")
    days = (deadline - today).days
    if days <= 0:
        return None
    return days if days in offsets else None


class ReminderService:
    def __init__(self, repository: ReminderStore, offsets: tuple[int, ...]) -> None:
        self.repository = repository
        self.offsets = offsets

    async def process(self, user_id: int, today: date, sender: ReminderSender) -> ReminderReport:
        sent = 0
        failed = 0
        for candidate in await self.repository.candidates(user_id):
            days = due_offset(candidate.deadline_date, today, self.offsets)
            if days is None or await self.repository.was_sent(user_id, candidate.deadline_id, days):
                continue
            try:
                await sender(candidate, days)
            except Exception:
                failed += 1
                continue
            await self.repository.record(user_id, candidate.deadline_id, days)
            sent += 1
        return ReminderReport(sent=sent, failed=failed)
