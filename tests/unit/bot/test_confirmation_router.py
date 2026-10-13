from types import SimpleNamespace

import pytest

from conference_scout.agent.actions import ActionUnavailable
from conference_scout.bot.confirmation_router import cancel_action, confirm_action
from conference_scout.schemas.agent import ActionResult


class Callback:
    def __init__(self, data):
        self.data = data
        self.message = self
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


@pytest.mark.asyncio
async def test_confirmation_is_owner_bound_and_duplicate_is_harmless() -> None:
    calls = []

    class Service:
        async def confirm(self, user_id, action_id):
            calls.append((user_id, action_id))
            if len(calls) > 1:
                raise ActionUnavailable("action already completed")
            return ActionResult(action_id=action_id, success=True, message="done")

    first, duplicate = Callback("action:confirm:12"), Callback("action:confirm:12")
    await confirm_action(first, Service(), owner_user_id=7)
    await confirm_action(duplicate, Service(), owner_user_id=7)

    assert calls == [(7, 12), (7, 12)]
    assert "уже" in duplicate.answers[-1][0].casefold()


@pytest.mark.asyncio
async def test_malformed_action_id_is_rejected_without_repository_access() -> None:
    service = SimpleNamespace()
    callback = Callback("action:confirm:" + "9" * 100)

    await confirm_action(callback, service, owner_user_id=7)

    assert "некоррект" in callback.answers[-1][0].casefold()


@pytest.mark.asyncio
async def test_cancel_calls_owner_scoped_service() -> None:
    calls = []

    class Service:
        async def cancel(self, user_id, action_id):
            calls.append((user_id, action_id))

    callback = Callback("action:cancel:4")
    await cancel_action(callback, Service(), owner_user_id=7)
    assert calls == [(7, 4)]
