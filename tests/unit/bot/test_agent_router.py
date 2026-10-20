import asyncio
import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from conference_scout.agent.capabilities import capability_help_text
from conference_scout.agent.runtime import AgentReply
from conference_scout.bot import agent_router
from conference_scout.bot.agent_router import (
    handle_agent_message,
    handle_help,
    handle_memory,
    handle_new,
)
from conference_scout.bot.keyboards import main_menu
from conference_scout.conferences.cycle import ConferenceCycleReport, CycleMode
from conference_scout.schemas.agent import DomainReference, PendingActionView
from conference_scout.schemas.common import AgentRunStatus, PendingActionStatus, ProgressEventType
from conference_scout.workflows.progress import ProgressEvent


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
        return FakeSentMessage(self, len(self.answers) - 1)


class FakeSentMessage:
    def __init__(self, owner: FakeMessage, index: int) -> None:
        self.owner = owner
        self.index = index

    async def edit_text(self, text, **kwargs):
        self.owner.answers[self.index] = (text, kwargs)


@pytest.mark.asyncio
async def test_workflow_reporter_rate_limits_edits_and_renders_safe_event() -> None:
    progress_module = importlib.import_module("conference_scout.workflows.progress")
    class Clock:
        value = 100.0

        def __call__(self):
            return self.value

    class StatusMessage:
        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **_kwargs):
            self.edits.append(text)

    clock = Clock()
    status = StatusMessage()
    reporter_type = agent_router.TelegramWorkflowReporter
    reporter = reporter_type(status, min_edit_interval=1.0, clock=clock)

    await reporter.update(
        progress_module.ProgressEvent(
            event_type="TOOL_STARTED", message="Открываю официальный сайт"
        )
    )
    await reporter.update(
        progress_module.ProgressEvent(
            event_type="FACTS_FOUND", message="Найдены даты проведения"
        )
    )
    clock.value += 1.0
    await reporter.update(
        progress_module.ProgressEvent(
            event_type="VALIDATION_RESULT", message="Проверены 3 даты"
        )
    )

    assert status.edits == [
        "ИНСТРУМЕНТ\nОткрываю официальный сайт",
        "ПРОВЕРКА\nПроверены 3 даты",
    ]


@pytest.mark.asyncio
async def test_arbitrary_text_is_direct_agent_chat_and_long_reply_is_split() -> None:
    class Runtime:
        async def respond(self, user_id, text, *, progress=None):
            assert (user_id, text) == (7, "hello")
            assert progress is not None
            await progress("Ищу конференции в интернете: страница 1…")
            return AgentReply(text="x" * 8_100)

    message = FakeMessage()
    await handle_agent_message(message, Runtime(), owner_user_id=7)

    assert message.typing == 1
    assert len(message.answers) == 4
    assert message.answers[0][0] == "Запрос обработан."
    assert all(len(text) <= 4_000 for text, _ in message.answers[1:])
    assert message.answers[-1][1]["reply_markup"].is_persistent is True


def test_main_menu_is_persistent() -> None:
    markup = main_menu()

    assert markup.is_persistent is True


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
        async def respond(self, *_args, **_kwargs):
            return AgentReply(
                text="Готово",
                pending_actions=(pending,),
                references=(DomainReference(kind="conference", id=3, position=1),),
            )

    message = FakeMessage()
    await handle_agent_message(message, Runtime(), owner_user_id=7)

    assert message.answers[0][0] == "Подготовлено действий: 1. Подтвердите кнопками ниже."
    assert message.answers[1][1]["reply_markup"].is_persistent is True
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


@pytest.mark.asyncio
async def test_help_and_explicit_capability_question_use_catalog_without_llm() -> None:
    class Runtime:
        async def respond(self, *_args, **_kwargs):
            raise AssertionError("LLM must not be called for an explicit capability question")

    help_message = FakeMessage("/help")
    capability_message = FakeMessage("Что ты умеешь?")

    await handle_help(help_message)
    await handle_agent_message(capability_message, Runtime(), owner_user_id=7)

    assert help_message.answers[0][0] == capability_help_text()
    assert capability_message.answers[0][0] == capability_help_text()
    assert help_message.answers[0][1]["reply_markup"].is_persistent is True
    assert capability_message.answers[0][1]["reply_markup"].is_persistent is True


def test_model_markdown_is_rendered_as_plain_text_with_numbering() -> None:
    text = "### **План**\n* Первый пункт\n* [Источник](https://example.org)\n`код`"

    assert agent_router.sanitize_model_text(text) == (
        "ПЛАН\n1. Первый пункт\n2. Источник — https://example.org\nкод"
    )


def test_inline_numbered_sections_and_bullets_are_split_into_readable_paragraphs() -> None:
    text = (
        "Я — ваш агент. Вот что я умею: 1. Поиск конференций Я проверяю источники. "
        "2. Мониторинг дедлайнов - Сроки подачи тезисов. - Сроки регистрации. "
        "3. Ведение календаря Я показываю ближайшие даты."
    )

    assert agent_router.sanitize_model_text(text) == (
        "Я — ваш агент. Вот что я умею:\n\n"
        "1. Поиск конференций Я проверяю источники.\n\n"
        "2. Мониторинг дедлайнов\n"
        "1. Сроки подачи тезисов.\n"
        "2. Сроки регистрации.\n\n"
        "3. Ведение календаря Я показываю ближайшие даты."
    )


def test_hyphens_inside_numbered_conference_name_are_not_rendered_as_bullets() -> None:
    text = (
        "4. Объявления о конференциях - ГАИШ - МГУ\n"
        "Изменений нет. Даты и дедлайны не найдены."
    )

    assert agent_router.sanitize_model_text(text) == text


@pytest.mark.asyncio
async def test_search_flow_clears_state_and_runs_unified_cycle_with_raw_query() -> None:
    class State:
        cleared = False

        async def clear(self):
            self.cleared = True

    class CycleService:
        async def run(self, user_id, request, progress=None):
            assert user_id == 7
            assert request.mode == CycleMode.MANUAL
            assert request.query == "IAU 2027"
            assert progress is not None
            await progress.emit(
                14,
                ProgressEvent(
                    event_type=ProgressEventType.TOOL_STARTED,
                    message="Проверяю отслеживаемые конференции.",
                ),
            )
            return ConferenceCycleReport(
                run_id=14,
                status=AgentRunStatus.COMPLETED,
                checked_tracked=3,
                updated_tracked=1,
                reactivated_candidates=2,
                new_candidate_ids=(41, 42),
                duplicates=4,
                pages_checked=5,
            )

    state = State()
    message = FakeMessage("IAU 2027")

    await agent_router.handle_search_query(
        message, state, CycleService(), owner_user_id=7
    )

    assert state.cleared is True
    assert "Новых кандидатов: 2" in message.answers[-1][0]
    assert "Номер запуска: 14" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_second_message_does_not_start_duplicate_long_search() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class Runtime:
        calls = 0

        async def respond(self, *_args, **_kwargs):
            self.calls += 1
            started.set()
            await release.wait()
            return AgentReply(text="Первый поиск завершён")

    runtime = Runtime()
    first = FakeMessage("Первый запрос")
    second = FakeMessage("Повторный запрос")
    first_task = asyncio.create_task(handle_agent_message(first, runtime, owner_user_id=7))
    await started.wait()

    await handle_agent_message(second, runtime, owner_user_id=7)
    release.set()
    await first_task

    assert runtime.calls == 1
    assert "предыдущий запрос" in second.answers[-1][0].casefold()
