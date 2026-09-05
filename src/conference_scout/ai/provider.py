from typing import Protocol


class LLMProvider(Protocol):
    async def complete(self, system_prompt: str, user_prompt: str) -> str: ...
