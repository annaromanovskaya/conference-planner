from collections.abc import Sequence
from typing import Protocol

from conference_scout.ai.models import AvailableModel, ChatMessage, ChatResponse, ToolDefinition


class ModelGatewayError(RuntimeError):
    def __init__(self, message: str, *, code: str = "upstream_error") -> None:
        super().__init__(message)
        self.code = code


class ModelGateway(Protocol):
    async def list_models(self) -> Sequence[AvailableModel]: ...

    async def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> ChatResponse: ...
