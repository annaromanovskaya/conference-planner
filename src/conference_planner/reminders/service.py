from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol


class NotificationKind(StrEnum):
    EXACT = "EXACT"
    APPROXIMATE_MONTH_START = "APPROXIMATE_MONTH_START"


@dataclass(frozen=True, slots=True)
class ReminderCandidate:
    deadline_id: int
    conference_name: str
    deadline_type: str
    deadline_date: date | None
    approximate_year: int | None = None
    approximate_month: int | None = None


@dataclass(frozen=True, slots=True)
class ReminderNotice:
    candidate: ReminderCandidate
    notification_kind: NotificationKind
    threshold: int
    days_remaining: int | None = None

    def telegram_text(self) -> str:
        label = _DEADLINE_LABELS.get(
            self.candidate.deadline_type, self.candidate.deadline_type.casefold()
        )
        if self.notification_kind == NotificationKind.APPROXIMATE_MONTH_START:
            month = _MONTHS[self.candidate.approximate_month or 1]
            return (
                f"НАПОМИНАНИЕ О ДЕДЛАЙНЕ\n\n{self.candidate.conference_name}\n"
                f"{label.capitalize()}: {month} {self.candidate.approximate_year}."
            )
        assert self.candidate.deadline_date is not None
        if self.days_remaining == 0:
            prefix = "Сегодня истекает"
        elif self.days_remaining == 1:
            prefix = "Завтра истекает"
        else:
            prefix = (
                f"Через {self.days_remaining} {_days_word(self.days_remaining)} истекает"
            )
        return (
            f"НАПОМИНАНИЕ О ДЕДЛАЙНЕ\n\n{self.candidate.conference_name}\n"
            f"{prefix} {label}: {self.candidate.deadline_date.strftime('%d.%m.%Y')}."
        )


_DEADLINE_LABELS = {
    "ABSTRACT_SUBMISSION": "срок подачи абстрактов",
    "ORAL_ABSTRACT_SUBMISSION": "срок подачи абстрактов для устных докладов",
    "REGISTRATION_OPEN": "открытие регистрации",
    "EARLY_REGISTRATION": "срок ранней регистрации",
    "REGISTRATION": "срок регистрации",
    "POSTER_SUBMISSION": "срок подачи постера",
    "PAYMENT": "срок оплаты",
    "ACCEPTANCE_NOTIFICATION": "уведомление о принятии",
    "OTHER": "дедлайн",
}

_MONTHS = {
    1: "январь",
    2: "февраль",
    3: "март",
    4: "апрель",
    5: "май",
    6: "июнь",
    7: "июль",
    8: "август",
    9: "сентябрь",
    10: "октябрь",
    11: "ноябрь",
    12: "декабрь",
}


def _days_word(value: int) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return "день"
    if value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


class ReminderStore(Protocol):
    async def candidates(self, user_id: int) -> list[ReminderCandidate]: ...

    async def was_sent(
        self, user_id: int, deadline_id: int, notification_kind: str, threshold: int
    ) -> bool: ...

    async def record(
        self, user_id: int, deadline_id: int, notification_kind: str, threshold: int
    ) -> None: ...


ReminderSender = Callable[[ReminderNotice], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReminderReport:
    sent: int = 0
    failed: int = 0


def due_offset(deadline: date, today: date, offsets: tuple[int, ...]) -> int | None:
    if not offsets or any(offset < 0 for offset in offsets):
        raise ValueError("reminder offsets must be non-negative")
    days = (deadline - today).days
    if days < 0:
        return None
    return days if days in offsets else None


class ReminderService:
    def __init__(self, repository: ReminderStore, offsets: tuple[int, ...]) -> None:
        if not offsets or any(offset < 0 for offset in offsets):
            raise ValueError("reminder offsets must be non-negative")
        self.repository = repository
        self.offsets = offsets

    async def process_daily(
        self, user_id: int, today: date, sender: ReminderSender
    ) -> ReminderReport:
        sent = 0
        failed = 0
        for candidate in await self.repository.candidates(user_id):
            notice = self.notice_for(candidate, today)
            if notice is None or await self.repository.was_sent(
                user_id,
                candidate.deadline_id,
                notice.notification_kind.value,
                notice.threshold,
            ):
                continue
            try:
                await sender(notice)
            except Exception:
                failed += 1
                continue
            await self.repository.record(
                user_id,
                candidate.deadline_id,
                notice.notification_kind.value,
                notice.threshold,
            )
            sent += 1
        return ReminderReport(sent=sent, failed=failed)

    def notice_for(
        self, candidate: ReminderCandidate, today: date
    ) -> ReminderNotice | None:
        if candidate.deadline_date is not None:
            remaining = (candidate.deadline_date - today).days
            if remaining < 0:
                return None
            crossed = sorted(offset for offset in self.offsets if offset >= remaining)
            if not crossed:
                return None
            return self.build_exact_notice(candidate, today, threshold=crossed[0])
        if (
            candidate.approximate_year == today.year
            and candidate.approximate_month == today.month
            and today.day == 1
        ):
            return ReminderNotice(
                candidate=candidate,
                notification_kind=NotificationKind.APPROXIMATE_MONTH_START,
                threshold=0,
            )
        return None

    @staticmethod
    def build_exact_notice(
        candidate: ReminderCandidate, today: date, *, threshold: int | None = None
    ) -> ReminderNotice | None:
        if candidate.deadline_date is None:
            return None
        remaining = (candidate.deadline_date - today).days
        if remaining < 0:
            return None
        return ReminderNotice(
            candidate=candidate,
            notification_kind=NotificationKind.EXACT,
            threshold=remaining if threshold is None else threshold,
            days_remaining=remaining,
        )
