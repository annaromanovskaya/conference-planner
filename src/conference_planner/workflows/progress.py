import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from conference_planner.db.models import AgentProgressEvent
from conference_planner.db.repositories.agent_workflows import AgentWorkflowRepository
from conference_planner.schemas.common import ProgressEventType

_SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "prompt",
    "raw_response",
    "model_response",
    "secret",
    "token",
)


class ProgressEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: ProgressEventType
    message: str = Field(min_length=1, max_length=1_000)
    safe_data: dict[str, str] = Field(default_factory=dict)


def sanitize_progress_data(data: Mapping[str, object]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in data.items():
        normalized_key = key.casefold().replace("-", "_")
        if any(fragment in normalized_key for fragment in _SENSITIVE_KEYS):
            continue
        sanitized[key[:100]] = _sanitize_text(str(value), limit=500)
    return sanitized


def _sanitize_text(value: str, *, limit: int) -> str:
    value = re.sub(r"(?i)bearer\s+\S+", "Bearer [скрыто]", value)
    value = re.sub(r"\bhub_[A-Za-z0-9._-]+", "hub_[скрыто]", value)
    return value[:limit]


class WorkflowProgress:
    def __init__(self, repository: AgentWorkflowRepository) -> None:
        self.repository = repository

    async def emit(self, run_id: int, event: ProgressEvent) -> None:
        await self.repository.append_progress(
            run_id,
            event.event_type,
            _sanitize_text(event.message, limit=1_000),
            sanitize_progress_data(event.safe_data),
        )

    async def list_after(
        self, run_id: int, sequence: int
    ) -> tuple[AgentProgressEvent, ...]:
        return tuple(await self.repository.list_progress_after(run_id, sequence))
