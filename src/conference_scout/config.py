from functools import cached_property
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Validated process configuration loaded exclusively from the environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    telegram_bot_token: SecretStr
    owner_telegram_id: int = Field(gt=0)
    openrouter_api_key: SecretStr
    openrouter_model: str = Field(min_length=1)
    database_url: str = "sqlite+aiosqlite:////data/conference_scout.db"
    searxng_url: HttpUrl = HttpUrl("http://searxng:8080")
    discovery_day: str = "sun"
    discovery_hour: int = Field(default=10, ge=0, le=23)
    monitoring_hour: int = Field(default=9, ge=0, le=23)
    reminder_days: Annotated[tuple[int, ...], NoDecode] = (30, 14, 7, 3, 1)
    min_relevance_score: float = Field(default=0.5, ge=0, le=1)
    app_timezone: str = "UTC"
    log_level: str = "INFO"
    request_timeout_seconds: float = Field(default=20, gt=0)
    max_page_bytes: int = Field(default=2_000_000, ge=1_024)
    max_page_text_chars: int = Field(default=50_000, ge=1_000)

    @field_validator("reminder_days", mode="before")
    @classmethod
    def parse_reminder_days(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("REMINDER_DAYS cannot be empty")
            try:
                parsed = tuple(int(item.strip()) for item in value.split(","))
            except ValueError as exc:
                raise ValueError("REMINDER_DAYS must contain comma-separated integers") from exc
            return parsed
        return value

    @field_validator("reminder_days")
    @classmethod
    def validate_reminder_days(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(day <= 0 for day in value):
            raise ValueError("reminder days must be positive")
        if len(set(value)) != len(value):
            raise ValueError("reminder days must be unique")
        return tuple(sorted(value, reverse=True))

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("APP_TIMEZONE must be an IANA timezone") from exc
        return value

    @cached_property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)
