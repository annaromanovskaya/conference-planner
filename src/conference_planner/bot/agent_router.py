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

from conference_planner.agent.capabilities import capability_help_text, is_capability_question
from conference_planner.agent.runtime import AgentReply, AgentRuntime
from conference_planner.bot.command_catalog import UNKNOWN_COMMAND_TEXT
from conference_planner.bot.keyboards import action_confirmation, main_menu
from conference_planner.conferences.cycle import (
    ConferenceCycleReport,
    ConferenceCycleService,
    CycleMode,
    CycleRequest,
)
from conference_planner.schemas.agent import SaveMemoryAction
from conference_planner.schemas.common import MemoryKind, MemoryScope, ProgressEventType
from conference_planner.workflows.progress import ProgressEvent

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
        from conference_planner.schemas.agent import ForgetMemoryAction

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

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message, state: FSMContext) -> None:
        await handle_unknown_command(message, state)

    @router.message(F.text)
    async def free_text(
        message: Message,
        state: FSMContext,
        agent_runtime,
        conference_cycle_service,
        conference_research_repository,
        conference_research_service,
        confirmation_service,
        discovery_controller,
        owner_user_id: int,
    ) -> None:
        await handle_free_text(
            message,
            state,
            agent_runtime,
            conference_cycle_service,
            owner_user_id=owner_user_id,
            candidate_repository=conference_research_repository,
            candidate_research_service=conference_research_service,
            confirmation_service=confirmation_service,
            discovery_controller=discovery_controller,
        )

    return router


async def handle_unknown_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(UNKNOWN_COMMAND_TEXT, reply_markup=main_menu())


async def handle_free_text(
    message: Message,
    state: FSMContext,
    agent_runtime: AgentRuntime,
    conference_cycle_service: ConferenceCycleService,
    *,
    owner_user_id: int,
    candidate_repository=None,
    candidate_research_service=None,
    confirmation_service=None,
    discovery_controller=None,
) -> None:
    text = message.text or ""
    if discovery_controller is not None and re.search(
        r"(какие|список|покажи|отчет|отчёт).{0,70}(страниц|ссылок|сайтов|проверен|искал|запросов)",
        text.casefold(),
    ):
        await message.answer(await discovery_controller.latest_search_report(owner_user_id))
        return
    memory_action = memory_action_from_text(text)
    if memory_action is not None and confirmation_service is not None:
        await state.clear()
        try:
            pending = await confirmation_service.propose(owner_user_id, memory_action)
        except Exception:
            await message.answer(
                "Не удалось подготовить запись в долговременную память. "
                "Ничего не сохранено; повторите запрос позже.",
                reply_markup=main_menu(),
            )
            return
        await message.answer(
            f"Требуется подтверждение:\n{pending.summary}",
            reply_markup=action_confirmation(pending.id),
        )
        return
    if _is_conference_discovery_request(text):
        await handle_search_query(
            message,
            state,
            conference_cycle_service,
            owner_user_id=owner_user_id,
            candidate_repository=candidate_repository,
            candidate_research_service=candidate_research_service,
            discovery_controller=discovery_controller,
        )
        return
    await handle_agent_message(message, agent_runtime, owner_user_id=owner_user_id)


def memory_action_from_text(text: str) -> SaveMemoryAction | None:
    statement = text.strip()
    if not statement:
        return None
    value = statement.casefold()
    explicit_memory = bool(
        re.search(r"\b(запомни(?:те)?|учти(?:те)?|сохрани(?:те)?\s+в\s+память)\b", value)
    )
    negative_instruction = bool(
        re.search(r"\bне\s+(предлагай(?:те)?|показывай(?:те)?|ищи(?:те)?)\b", value)
    )
    owner_profile = bool(
        re.search(
            r"\b(я\s+занимаюсь|я\s+интересуюсь|мне\s+интересн(?:а|о|ы)|предпочитаю)\b",
            value,
        )
    )
    if not (explicit_memory or negative_instruction or owner_profile):
        return None

    if negative_instruction:
        kind = MemoryKind.INSTRUCTION
    elif owner_profile or "приоритет" in value or "предпоч" in value:
        kind = MemoryKind.PREFERENCE
    else:
        kind = MemoryKind.NOTE
    return SaveMemoryAction(
        kind=kind,
        scope=MemoryScope.GLOBAL,
        original_text=statement,
        rule_data={"statement": statement},
        provenance={"source": "owner_message", "capture": "deterministic"},
    )


def _is_conference_discovery_request(text: str) -> bool:
    value = text.casefold()
    if re.search(r"https?://", value):
        return False
    asks_to_discover = bool(
        re.search(
            r"\b(поищ(?:и|ите)|ищ(?:и|ите)|найд(?:и|ите)|поиск|find|search)\b",
            value,
        )
    )
    conference_context = bool(
        re.search(r"\b(конференц|мероприят|воркшоп|conference|event|workshop)\w*", value)
    )
    return asks_to_discover and conference_context


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
    text = sanitize_model_text(reply.text)
    model_label = local_model_response_label(reply)
    if model_label is not None:
        text = f"{text}\n\nМодель ответа: {model_label}"
    chunks = split_message(text)
    for index, chunk in enumerate(chunks):
        markup = main_menu() if index == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup)
    for pending in reply.pending_actions:
        await message.answer(
            f"Требуется подтверждение:\n{pending.summary}",
            reply_markup=action_confirmation(pending.id),
        )


def local_model_response_label(reply: AgentReply) -> str | None:
    if reply.model_profile_id != "llm-local":
        return None
    if reply.model_name:
        return reply.model_name
    requested = reply.requested_model_name or "неизвестна"
    return f"{requested} — сервер не сообщил фактическую модель"


async def handle_search_query(
    message: Message,
    state: FSMContext,
    conference_cycle_service: ConferenceCycleService,
    *,
    owner_user_id: int,
    candidate_repository=None,
    candidate_research_service=None,
    discovery_controller=None,
) -> None:
    await state.clear()
    query = (message.text or "").strip()
    if discovery_controller is not None:
        await discovery_controller.enqueue_query(owner_user_id, query, message)
        return
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
        if report.new_candidate_ids and candidate_repository is not None:
            from conference_planner.bot.conference_router import send_next_candidate

            await send_next_candidate(
                message,
                candidate_repository,
                owner_user_id,
                research_service=candidate_research_service,
            )


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
        f"Ошибки проверки отслеживаемых: {len(report.monitoring_errors)}",
        f"Ошибки поиска: {len(report.search_errors)}",
    ]
    if report.monitoring_errors:
        lines.extend(("", "Коды проверки: " + ", ".join(report.monitoring_errors)))
    if report.search_errors:
        lines.extend(("", "Коды поиска: " + ", ".join(report.search_errors)))
    search_code = next(
        (
            code
            for code in report.search_errors
            if code in {"SEARCH_EMPTY", "SEARCH_UNAVAILABLE"}
        ),
        None,
    )
    if search_code is not None:
        lines.extend(("", f"Код поиска: {search_code}"))
        lines.append(f"Попыток поиска: {report.search_attempts}")
        if report.unresponsive_engines:
            lines.append("Недоступные движки:")
            lines.extend(f"- {item}" for item in report.unresponsive_engines)
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
