from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

NowProvider = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ApplicationClock:
    timezone: ZoneInfo
    started_at: datetime
    _now_provider: NowProvider

    @classmethod
    def start(
        cls,
        timezone: ZoneInfo,
        *,
        now_provider: NowProvider | None = None,
    ) -> "ApplicationClock":
        provider = now_provider or (lambda: datetime.now(UTC))
        started_at = cls._localize(provider(), timezone)
        return cls(timezone=timezone, started_at=started_at, _now_provider=provider)

    def now(self) -> datetime:
        return self._localize(self._now_provider(), self.timezone)

    def today(self) -> date:
        return self.now().date()

    @staticmethod
    def _localize(value: datetime, timezone: ZoneInfo) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone)
        return value.astimezone(timezone)
