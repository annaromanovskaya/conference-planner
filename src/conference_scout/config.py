from functools import cached_property
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from conference_scout.schemas.common import ModelRole


class Settings(BaseSettings):
    """Validated process configuration loaded exclusively from the environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    telegram_bot_token: SecretStr
    owner_telegram_id: int = Field(gt=0)
    ollama_cloud_base_url: HttpUrl = HttpUrl("https://ollama.com")
    ollama_cloud_api_key: SecretStr | None = None
    ollama_cloud_free_models: Annotated[tuple[str, ...], NoDecode] = (
        "gemma4:31b",
        "gpt-oss:120b",
        "gpt-oss:20b",
        "nemotron-3-nano:30b",
        "nemotron-3-super",
        "nemotron-3-ultra",
    )
    llm_hub_base_url: HttpUrl | None = None
    llm_hub_api_key: SecretStr | None = None
    agent_max_steps: int = Field(default=6, ge=1, le=20)
    agent_max_tool_calls_per_step: int = Field(default=3, ge=1, le=20)
    agent_timeout_seconds: float = Field(default=300, gt=0, le=600)
    tracked_check_timeout_seconds: float = Field(default=300, gt=0, le=600)
    agent_max_context_chars: int = Field(default=24_000, ge=1_000)
    agent_max_tool_result_chars: int = Field(default=12_000, ge=1_000)
    agent_supervisor_model: str | None = None
    agent_research_model: str | None = None
    agent_verifier_model: str | None = None
    agent_fallback_model: str | None = None
    agent_workflows_enabled: bool = True
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8765, ge=1, le=65_535)
    database_url: str = "sqlite+aiosqlite:////data/conference_scout.db"
    searxng_url: HttpUrl = HttpUrl("http://searxng:8080")
    reader_fallback_url: HttpUrl | None = HttpUrl("https://r.jina.ai")
    discovery_day: str = "sun"
    discovery_hour: int = Field(default=10, ge=0, le=23)
    reminder_hour: int = Field(default=9, ge=0, le=23)
    monitoring_hour: int = Field(default=9, ge=0, le=23)
    monitoring_interval_hours: int = Field(default=72, ge=24, le=720)
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

    @field_validator("ollama_cloud_free_models", mode="before")
    @classmethod
    def parse_cloud_models(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("OLLAMA_CLOUD_FREE_MODELS cannot be empty")
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("llm_hub_base_url", mode="before")
    @classmethod
    def empty_llm_hub_url_is_disabled(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("reader_fallback_url", mode="before")
    @classmethod
    def empty_reader_url_is_disabled(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "agent_supervisor_model",
        "agent_research_model",
        "agent_verifier_model",
        "agent_fallback_model",
        mode="before",
    )
    @classmethod
    def empty_role_model_uses_active_model(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ollama_cloud_free_models")
    @classmethod
    def validate_cloud_models(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("cloud model allowlist must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("cloud model allowlist must be unique")
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

    @property
    def model_role_overrides(self) -> dict[ModelRole, str]:
        values = {
            ModelRole.SUPERVISOR: self.agent_supervisor_model,
            ModelRole.RESEARCH: self.agent_research_model,
            ModelRole.VERIFIER: self.agent_verifier_model,
            ModelRole.FALLBACK: self.agent_fallback_model,
        }
        return {role: model for role, model in values.items() if model}
