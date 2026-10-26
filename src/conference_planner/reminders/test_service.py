import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from conference_planner.db.models import TestReminderTask
from conference_planner.db.repositories.test_reminders import TestReminderRepository
from conference_planner.reminders.service import ReminderCandidate, ReminderService
from conference_planner.schemas.common import DeadlineType, TestReminderStatus

logger = logging.getLogger(__name__)

_RELATIVE_PATTERN = re.compile(
    r"^через\s+(?P<count>\d+)\s+"
    r"(?P<unit>минуту|минуты|минут|час|часа|часов|день|дня|дней)$",
    re.IGNORECASE,
)
_EXACT_FORMAT = "%d.%m.%Y %H:%M"
_MIN_DELAY = timedelta(minutes=1)
_MAX_DELAY = timedelta(days=30)
_MAX_NAME_LENGTH = 200
_FAILED_NOTICE = (
    "Не удалось отправить тестовое напоминание после трёх попыток. "
    "Задание сохранено для диагностики; удалите его командой "
    "/cancel_test_reminder."
)


def _validate_send_at(send_at: datetime, now: datetime) -> datetime:
    if send_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("Время отправки и текущее время должны содержать временную зону.")
    normalized = send_at.astimezone(UTC)
    delay = normalized - now.astimezone(UTC)
    if not _MIN_DELAY <= delay <= _MAX_DELAY:
        raise ValueError(
            "Время отправки должно быть не раньше чем через минуту и не позже 30 дней."
        )
    return normalized


def parse_test_send_at(value: str, *, now: datetime, timezone: ZoneInfo) -> datetime:
    if now.tzinfo is None:
        raise ValueError("current time must include a timezone")
    normalized = " ".join(value.split())
    match = _RELATIVE_PATTERN.fullmatch(normalized)
    if match:
        count = int(match.group("count"))
        unit = match.group("unit").casefold()
        if unit.startswith("минут"):
            delay = timedelta(minutes=count)
        elif unit.startswith("час"):
            delay = timedelta(hours=count)
        else:
            delay = timedelta(days=count)
        result = now.astimezone(UTC) + delay
    else:
        try:
            local = datetime.strptime(normalized, _EXACT_FORMAT).replace(tzinfo=timezone)
        except ValueError as exc:
            raise ValueError(
                "Укажите время как ДД.ММ.ГГГГ ЧЧ:ММ или «через N минут/часов/дней»."
            ) from exc
        result = local.astimezone(UTC)
    return _validate_send_at(result, now)


@dataclass(frozen=True, slots=True)
class TestReminderDraft:
    name: str
    deadline_type: DeadlineType
    deadline_date: date
    send_at: datetime


@dataclass(frozen=True, slots=True)
class TestReminderReport:
    sent: int = 0
    failed: int = 0


TestReminderSender = Callable[[int, str], Awaitable[None]]


class TestReminderService:
    def __init__(self, repository: TestReminderRepository, timezone: ZoneInfo) -> None:
        self.repository = repository
        self.timezone = timezone

    async def schedule(
        self, user_id: int, draft: TestReminderDraft, *, now: datetime
    ) -> TestReminderTask:
        name = draft.name.strip()
        if not name:
            raise ValueError("Название конференции не может быть пустым.")
        if len(name) > _MAX_NAME_LENGTH:
            raise ValueError("Название конференции не должно быть длиннее 200 символов.")
        send_at = _validate_send_at(draft.send_at, now)
        deadline_end = datetime.combine(
            draft.deadline_date, time.max, tzinfo=self.timezone
        ).astimezone(UTC)
        if send_at > deadline_end:
            raise ValueError("Напоминание нельзя отправить после дня дедлайна.")
        task = await self.repository.create(
            user_id,
            name=name,
            deadline_type=draft.deadline_type,
            deadline_date=draft.deadline_date,
            send_at=send_at,
        )
        await self.repository.commit()
        return task

    async def process_due(
        self, now: datetime, sender: TestReminderSender
    ) -> TestReminderReport:
        if now.tzinfo is None:
            raise ValueError("current time must include a timezone")
        sent = 0
        failed = 0
        for task in await self.repository.due(now.astimezone(UTC)):
            try:
                record = await self.repository.record(task)
                candidate = ReminderCandidate(
                    deadline_id=record.deadline_id,
                    conference_name=record.conference_name,
                    deadline_type=record.deadline_type.value,
                    deadline_date=record.deadline_date,
                )
                scheduled_date = task.send_at.astimezone(self.timezone).date()
                notice = ReminderService.build_exact_notice(candidate, scheduled_date)
                if notice is None:
                    raise ValueError("test reminder deadline has passed")
                await sender(record.user_id, notice.telegram_text())
            except Exception as exc:
                failed_task = await self.repository.record_failure(task.id, str(exc))
                await self.repository.commit()
                failed += 1
                if failed_task.status == TestReminderStatus.FAILED:
                    try:
                        await sender(failed_task.user_id, _FAILED_NOTICE)
                    except Exception:
                        logger.warning(
                            "test reminder terminal failure notification failed",
                            exc_info=True,
                        )
                continue
            await self.repository.cleanup(record.user_id, task.id)
            await self.repository.commit()
            sent += 1
        return TestReminderReport(sent=sent, failed=failed)

    async def cancel(self, user_id: int) -> bool:
        task = await self.repository.active_for(user_id)
        if task is None:
            return False
        await self.repository.cleanup(user_id, task.id)
        await self.repository.commit()
        return True
