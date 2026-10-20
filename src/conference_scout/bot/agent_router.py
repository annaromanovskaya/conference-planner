import asyncio
import re
from collections.abc import Callable
from html import escape
from time import monotonic

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from conference_scout.agent.capabilities import capability_help_text, is_capability_question
from conference_scout.agent.runtime import AgentReply, AgentRuntime
from conference_scout.bot.keyboards import action_confirmation, main_menu
from conference_scout.conferences.cycle import (
    ConferenceCycleReport,
    ConferenceCycleService,
    CycleMode,
    CycleRequest,
)
from conference_scout.schemas.common import ProgressEventType
from conference_scout.workflows.progress import ProgressEvent

TELEGRAM_TEXT_LIMIT = 4_000
PROGRESS_HEARTBEAT_SECONDS = 15
_CHAT_LOCKS: dict[int, asyncio.Lock] = {}


class TelegramWorkflowReporter:
    def __init__(
        self,
        status_message,
        *,
        min_edit_interval: float = 0.75,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.status_message = status_message
        self.last_text = ""
        self.min_edit_interval = min_edit_interval
        self.clock = clock
        self._last_edit_at: float | None = None
        self._edit_lock = asyncio.Lock()

    async def update(self, event: ProgressEvent | str) -> None:
        structured = isinstance(event, ProgressEvent)
        text = render_progress_event(event) if structured else event
        if not text or text == self.last_text:
            return
        async with self._edit_lock:
            if text == self.last_text:
                return
            now = self.clock()
            final_event = structured and event.event_type in {
                ProgressEventType.RUN_COMPLETED,
                ProgressEventType.RUN_PARTIAL,
            }
            if (
                structured
                and not final_event
                and self._last_edit_at is not None
                and now - self._last_edit_at < self.min_edit_interval
            ):
                return
            try:
                await self.status_message.edit_text(text)
            except Exception:
                return
            self.last_text = text
            self._last_edit_at = now

    async def emit(self, _run_id: int, event: ProgressEvent) -> None:
        await self.update(event)

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(PROGRESS_HEARTBEAT_SECONDS)
            await self.update(
                "Поиск продолжается: источник или модель отвечают медленнее обычного…"
            )


TelegramProgressReporter = TelegramWorkflowReporter


def render_progress_event(event: ProgressEvent) -> str:
    labels = {
        ProgressEventType.AGENT_STARTED: "АГЕНТ",
        ProgressEventType.TOOL_STARTED: "ИНСТРУМЕНТ",
        ProgressEventType.TOOL_COMPLETED: "ИНСТРУМЕНТ",
        ProgressEventType.PAGE_OPENED: "СТРАНИЦА",
        ProgressEventType.FACTS_FOUND: "НАЙДЕННЫЕ ДАННЫЕ",
        ProgressEventType.RECIPE_REUSED: "ПАРСИНГ",
        ProgressEventType.RECIPE_PROPOSED: "ПАРСИНГ",
        ProgressEventType.VALIDATION_RESULT: "ПРОВЕРКА",
        ProgressEventType.TASK_RETRY: "ПОВТОР",
        ProgressEventType.RUN_COMPLETED: "ГОТОВО",
        ProgressEventType.RUN_PARTIAL: "ЧАСТИЧНЫЙ РЕЗУЛЬТАТ",
    }
    return f"{labels[event.event_type]}\n{escape(event.message)}"


def build_agent_router() -> Router:
    router = Router(name="agent-chat")

    @router.message(Command("new"))
    async def new_command(
        message: Message, state: FSMContext, memory_service, owner_user_id: int
    ) -> None:
        await state.clear()
        await handle_new(message, memory_service, owner_user_id=owner_user_id)

    @router.message(Command("memory"))
    async def memory_command(
        message: Message, state: FSMContext, memory_service, owner_user_id: int
    ) -> None:
        await state.clear()
        await handle_memory(message, memory_service, owner_user_id=owner_user_id)

    @router.message(Command("forget"))
    async def forget_command(
        message: Message, state: FSMContext, confirmation_service, owner_user_id: int
    ) -> None:
        await state.clear()
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
    async def help_command(message: Message, state: FSMContext) -> None:
        await state.clear()
        await handle_help(message)

    @router.message(F.text)
    async def free_text(message: Message, agent_runtime, owner_user_id: int) -> None:
        await handle_agent_message(message, agent_runtime, owner_user_id=owner_user_id)

    return router


async def handle_agent_message(
    message: Message, agent_runtime: AgentRuntime, *, owner_user_id: int
) -> None:
    if is_capability_question(message.text or ""):
        await handle_help(message)
        return
    await _run_agent_request(
        message,
        agent_runtime,
        owner_user_id=owner_user_id,
        text=message.text or "",
    )


async def handle_help(message: Message) -> None:
    await message.answer(capability_help_text(), reply_markup=main_menu())


async def send_agent_reply(message: Message, reply) -> None:
    chunks = split_message(sanitize_model_text(reply.text))
    for index, chunk in enumerate(chunks):
        markup = main_menu() if index == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup)
    for pending in reply.pending_actions:
        await message.answer(
            f"Требуется подтверждение:\n{pending.summary}",
            reply_markup=action_confirmation(pending.id),
        )


async def handle_search_query(
    message: Message,
    state: FSMContext,
    conference_cycle_service: ConferenceCycleService,
    *,
    owner_user_id: int,
) -> None:
    await state.clear()
    query = (message.text or "").strip()
    lock = _CHAT_LOCKS.setdefault(message.chat.id, asyncio.Lock())
    if lock.locked():
        await message.answer(
            "Цикл проверки уже выполняется. Дождитесь итогового отчёта.",
            reply_markup=main_menu(),
        )
        return
    async with lock:
        await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        status_message = await message.answer("Запускаю полный цикл проверки конференций…")
        reporter = TelegramWorkflowReporter(status_message)
        heartbeat = asyncio.create_task(reporter.heartbeat())
        try:
            report = await conference_cycle_service.run(
                owner_user_id,
                CycleRequest(mode=CycleMode.MANUAL, query=query),
                progress=reporter,
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        await reporter.update("Цикл проверки завершён.")
        await message.answer(format_cycle_report(report), reply_markup=main_menu())


def format_cycle_report(report: ConferenceCycleReport) -> str:
    lines = [
        "ПРОВЕРКА КОНФЕРЕНЦИЙ ЗАВЕРШЕНА",
        "",
        f"Проверено отслеживаемых: {report.checked_tracked}",
        f"Обновлено отслеживаемых: {report.updated_tracked}",
        f"Возвращено пропущенных: {report.reactivated_candidates}",
        f"Новых кандидатов: {len(report.new_candidate_ids)}",
        f"Исключено дублей: {report.duplicates}",
        f"Проверено страниц поиска: {report.pages_checked}",
        f"Ошибок: {len(report.errors)}",
    ]
    if report.new_candidate_ids:
        lines.extend(("", "Нажмите «Следующий кандидат», чтобы начать рассмотрение."))
    if report.errors:
        lines.extend(("", "Коды ошибок: " + ", ".join(report.errors)))
    lines.extend(("", f"Номер запуска: {report.run_id}."))
    return "\n".join(lines)


async def _run_agent_request(
    message: Message,
    agent_runtime: AgentRuntime,
    *,
    owner_user_id: int,
    text: str,
) -> None:
    lock = _CHAT_LOCKS.setdefault(message.chat.id, asyncio.Lock())
    if lock.locked():
        await message.answer(
            "Предыдущий запрос ещё выполняется. Дождитесь результата или сообщения об ошибке.",
            reply_markup=main_menu(),
        )
        return
    async with lock:
        await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        status_message = await message.answer("Анализирую запрос…")
        reporter = TelegramProgressReporter(status_message)
        heartbeat = asyncio.create_task(reporter.heartbeat())
        try:
            reply = await agent_runtime.respond(
                owner_user_id,
                text,
                progress=reporter.update,
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        await reporter.update(completion_status(reply))
        await send_agent_reply(message, reply)


def completion_status(reply: AgentReply) -> str:
    if reply.error_code is not None:
        return "Запрос завершён с ошибкой."
    if reply.pending_actions:
        count = len(reply.pending_actions)
        return f"Подготовлено действий: {count}. Подтвердите кнопками ниже."
    return "Запрос обработан."


async def handle_new(message: Message, memory_service, *, owner_user_id: int) -> None:
    await memory_service.start_new_conversation(owner_user_id)
    await message.answer(
        "Начат новый диалог. Долгосрочная память сохранена.", reply_markup=main_menu()
    )


async def handle_memory(message: Message, memory_service, *, owner_user_id: int) -> None:
    memories = await memory_service.list_memories(owner_user_id)
    text = (
        "\n".join(f"{item.id}. {escape(item.original_text)}" for item in memories)
        or "Долгосрочная память пока пуста."
    )
    for chunk in split_message(text):
        await message.answer(chunk, reply_markup=main_menu())


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


def sanitize_model_text(text: str) -> str:
    text = re.sub(r"\[([^\]]+)]\((https?://[^)]+)\)", r"\1 — \2", text)
    # Some models flatten otherwise valid lists into one long line. Restore only
    # unambiguous section and bullet boundaries; decimals, dates and URLs stay intact.
    text = re.sub(
        r"(?<!\n)\s+(?=(?:[1-9]\d{0,2})\.\s+[A-ZА-ЯЁ])",
        "\n\n",
        text,
    )
    normalized_lines: list[str] = []
    for line in text.splitlines():
        # Restore flattened Markdown bullets only when the line also contains a
        # punctuated bullet boundary. Plain names such as
        # A conference title with internal hyphens must stay intact.
        if re.search(r"[.!?:]\s+-\s+(?=[A-ZА-ЯЁ])", line):
            line = re.sub(r"\s+-\s+(?=[A-ZА-ЯЁ])", "\n* ", line)
        normalized_lines.append(line)
    text = "\n".join(normalized_lines)
    lines: list[str] = []
    list_index = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            line = heading.group(1).upper()
            list_index = 0
        elif re.match(r"^[*+-]\s+", line):
            list_index += 1
            line = re.sub(r"^[*+-]\s+", "", line)
            line = f"{list_index}. {line}"
        else:
            if line:
                list_index = 0
            line = re.sub(r"^>\s?", "", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = line.replace("*", "")
        lines.append(line.rstrip())
    return "\n".join(lines).strip()
