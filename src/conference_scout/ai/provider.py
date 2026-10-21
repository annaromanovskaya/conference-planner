from typing import Any, Protocol


class LLMProvider(Protocol):
    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> str: ...
