from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from conference_scout.agent.runtime import AgentReply
from conference_scout.bot.agent_router import handle_agent_message, handle_memory, handle_new
from conference_scout.schemas.agent import DomainReference, PendingActionView
from conference_scout.schemas.common import PendingActionStatus


class FakeMessage:
    def __init__(self, text="hello"):
        self.text = text
        self.answers = []
        self.chat = SimpleNamespace(id=101)
        self.bot = SimpleNamespace(send_chat_action=self._typing)
        self.typing = 0

    async def _typing(self, **_kwargs):
        self.typing += 1

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


@pytest.mark.asyncio
async def test_arbitrary_text_is_direct_agent_chat_and_long_reply_is_split() -> None:
    class Runtime:
        async def respond(self, user_id, text):
            assert (user_id, text) == (7, "hello")
            return AgentReply(text="x" * 8_100)

    message = FakeMessage()
    await handle_agent_message(message, Runtime(), owner_user_id=7)

    assert message.typing == 1
    assert len(message.answers) == 3
    assert all(len(text) <= 4_000 for text, _ in message.answers)


@pytest.mark.asyncio
async def test_pending_actions_render_confirmation_buttons_and_references() -> None:
    pending = PendingActionView(
        id=44,
        action_type="track_conference",
        summary="Отслеживать конференцию 3",
        status=PendingActionStatus.PENDING,
        expires_at=datetime(2026, 10, 13, tzinfo=UTC),
    )

    class Runtime:
        async def respond(self, *_args):
            return AgentReply(
                text="Готово",
                pending_actions=(pending,),
                references=(DomainReference(kind="conference", id=3, position=1),),
            )

    message = FakeMessage()
    await handle_agent_message(message, Runtime(), owner_user_id=7)

    markup = message.answers[-1][1]["reply_markup"]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks == ["action:confirm:44", "action:cancel:44"]
    assert "Отслеживать" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_new_and_memory_commands_use_owner_scoped_service() -> None:
    calls = []

    class Memory:
        async def start_new_conversation(self, user_id):
            calls.append(("new", user_id))

        async def list_memories(self, user_id):
            calls.append(("memory", user_id))
            return [SimpleNamespace(id=5, original_text="Не предлагай США")]

    new_message, memory_message = FakeMessage(), FakeMessage()
    await handle_new(new_message, Memory(), owner_user_id=7)
    await handle_memory(memory_message, Memory(), owner_user_id=7)

    assert calls == [("new", 7), ("memory", 7)]
    assert "Не предлагай США" in memory_message.answers[0][0]
