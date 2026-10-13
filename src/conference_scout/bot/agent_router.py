from html import escape

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import Message

from conference_scout.agent.runtime import AgentRuntime
from conference_scout.bot.keyboards import action_confirmation

TELEGRAM_TEXT_LIMIT = 4_000


def build_agent_router() -> Router:
    router = Router(name="agent-chat")

    @router.message(Command("new"))
    async def new_command(message: Message, memory_service, owner_user_id: int) -> None:
        await handle_new(message, memory_service, owner_user_id=owner_user_id)

    @router.message(Command("memory"))
    async def memory_command(message: Message, memory_service, owner_user_id: int) -> None:
        await handle_memory(message, memory_service, owner_user_id=owner_user_id)

    @router.message(Command("forget"))
    async def forget_command(message: Message, confirmation_service, owner_user_id: int) -> None:
        from conference_scout.schemas.agent import ForgetMemoryAction

        raw_id = (message.text or "").partition(" ")[2].strip()
        if not raw_id.isdigit() or not 0 < len(raw_id) <= 18:
            await message.answer("Формат: /forget ID — ID можно посмотреть через /memory.")
            return
        pending = await confirmation_service.propose(
            owner_user_id, ForgetMemoryAction(memory_id=int(raw_id))
        )
        await message.answer(pending.summary, reply_markup=action_confirmation(pending.id))

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Пишите вопрос обычным сообщением. Команды: /model — модель, /new — новый "
            "диалог, /memory — память, /forget ID — забыть запись после подтверждения."
        )

    @router.message(F.text)
    async def free_text(message: Message, agent_runtime, owner_user_id: int) -> None:
        await handle_agent_message(message, agent_runtime, owner_user_id=owner_user_id)

    return router


async def handle_agent_message(
    message: Message, agent_runtime: AgentRuntime, *, owner_user_id: int
) -> None:
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    reply = await agent_runtime.respond(owner_user_id, message.text or "")
    for chunk in split_message(reply.text):
        await message.answer(chunk)
    for pending in reply.pending_actions:
        await message.answer(
            f"Требуется подтверждение:\n{pending.summary}",
            reply_markup=action_confirmation(pending.id),
        )


async def handle_new(message: Message, memory_service, *, owner_user_id: int) -> None:
    await memory_service.start_new_conversation(owner_user_id)
    await message.answer("Начат новый диалог. Долгосрочная память сохранена.")


async def handle_memory(message: Message, memory_service, *, owner_user_id: int) -> None:
    memories = await memory_service.list_memories(owner_user_id)
    text = (
        "\n".join(f"{item.id}. {escape(item.original_text)}" for item in memories)
        or "Долгосрочная память пока пуста."
    )
    for chunk in split_message(text):
        await message.answer(chunk)


def split_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> tuple[str, ...]:
    if not text:
        return ("Не удалось сформировать ответ.",)
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:].lstrip("\n")
    return tuple(chunks)
