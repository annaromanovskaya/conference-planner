from dataclasses import dataclass

import pytest

from conference_scout.ai.gateway import ModelGatewayError
from conference_scout.bot.errors import ExternalErrorMiddleware


@dataclass
class FakeMessage:
    answers: list[str]

    async def answer(self, text: str) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_provider_failure_is_reported_to_user_without_details() -> None:
    event = FakeMessage([])

    async def handler(_event, _data):
        raise ModelGatewayError("secret backend details")

    result = await ExternalErrorMiddleware()(handler, event, {})

    assert result is None
    assert len(event.answers) == 1
    assert "модел" in event.answers[0].casefold()
    assert "secret backend details" not in event.answers[0]
