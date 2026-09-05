from dataclasses import dataclass

import pytest

from conference_scout.ai.openrouter import OpenRouterError
from conference_scout.bot.errors import ExternalErrorMiddleware


@dataclass
class FakeMessage:
    answers: list[str]

    async def answer(self, text: str) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_openrouter_failure_is_reported_to_user() -> None:
    event = FakeMessage([])

    async def handler(_event, _data):
        raise OpenRouterError("secret backend details")

    result = await ExternalErrorMiddleware()(handler, event, {})

    assert result is None
    assert len(event.answers) == 1
    assert "OpenRouter" in event.answers[0]
    assert "secret backend details" not in event.answers[0]
