from dataclasses import dataclass

import pytest

from conference_scout.bot.access import OwnerOnlyFilter, private_instance_message


@dataclass
class User:
    id: int


@dataclass
class Event:
    from_user: User | None


@pytest.mark.asyncio
async def test_owner_is_allowed() -> None:
    assert await OwnerOnlyFilter(123)(Event(User(123))) is True


@pytest.mark.asyncio
async def test_non_owner_and_missing_user_are_rejected() -> None:
    gate = OwnerOnlyFilter(123)
    assert await gate(Event(User(999))) is False
    assert await gate(Event(None)) is False


def test_rejection_message_reveals_no_configuration() -> None:
    text = private_instance_message()
    assert "123" not in text
    assert "OWNER_TELEGRAM_ID" not in text
