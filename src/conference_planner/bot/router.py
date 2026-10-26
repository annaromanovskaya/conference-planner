from html import escape
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from conference_planner.bot.access import OwnerOnlyMiddleware
from conference_planner.bot.agent_router import build_agent_router, handle_search_query
from conference_planner.bot.conference_router import build_conference_router
from conference_planner.bot.confirmation_router import build_confirmation_router
from conference_planner.bot.errors import ExternalErrorMiddleware
from conference_planner.bot.keyboards import (
    action_confirmation,
    add_buttons,
    main_menu,
    topic_confirmation,
)
from conference_planner.bot.model_router import build_model_router
from conference_planner.bot.states import SearchFlow, SourceFlow, TopicFlow
from conference_planner.bot.test_reminder_router import build_test_reminder_router
from conference_planner.bot.usage_router import build_usage_router
from conference_planner.db.repositories.sources import source_display_name
from conference_planner.schemas.common import SourceType

MAIN_MENU_BUTTONS = frozenset(
    {
        "Мои темы",
        "Найти конференции",
        "Конференции",
        "Ближайшие дедлайны",
        "Следующий кандидат",
        "Источники",
        "Настройки",
    }
)

NEXT_CANDIDATE_TEXTS = frozenset(
    {
        "дальше",
        "следующий кандидат",
        "начинай верификацию",
        "давай дальше",
    }
)


def build_router(*, owner_telegram_id: int) -> Router:
    router = Router(name="owner")
    router.message.middleware(ExternalErrorMiddleware())
    router.callback_query.middleware(ExternalErrorMiddleware())
    router.message.middleware(OwnerOnlyMiddleware(owner_telegram_id))
    router.callback_query.middleware(OwnerOnlyMiddleware(owner_telegram_id))

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "🔭 Conference Planner\n\nЯ — ваш AI-агент по научным конференциям. "
            "Просто напишите вопрос или задачу. /model — выбрать модель, /help — помощь.",
            reply_markup=main_menu(),
        )

    @router.message(Command("cancel"))
    async def cancel_command(message: Message, state: FSMContext) -> None:
        await cancel_flow(message, state)

    @router.message(F.text == "Мои темы")
    async def topics(message: Message, state: FSMContext, topic_repository) -> None:
        await open_menu(state)
        values = await topic_repository.list_for_user(topic_repository.owner_user_id)
        text = "\n".join(f"⭐ {escape(item.name)}" for item in values) or "Тем пока нет."
        await message.answer(text, reply_markup=add_buttons("topic"), parse_mode="HTML")

    @router.message(F.text == "Найти конференции")
    async def find_conferences(message: Message, state: FSMContext) -> None:
        await open_menu(state)
        await state.set_state(SearchFlow.waiting_query)
        await message.answer("Введите тему, название конференции, год или ссылку для поиска.")

    @router.message(SearchFlow.waiting_query, F.text.func(is_flow_text))
    async def search_query(
        message: Message,
        state: FSMContext,
        conference_cycle_service,
        conference_research_repository,
        conference_research_service,
        owner_user_id: int,
    ) -> None:
        await handle_search_query(
            message,
            state,
            conference_cycle_service,
            owner_user_id=owner_user_id,
            candidate_repository=conference_research_repository,
            candidate_research_service=conference_research_service,
        )

    @router.callback_query(F.data == "topic:add")
    async def topic_add(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(TopicFlow.waiting_text)
        await callback.message.answer("Опишите научную тематику свободным текстом.")
        await callback.answer()

    @router.message(TopicFlow.waiting_text, F.text.func(is_flow_text))
    async def topic_preview(message: Message, state: FSMContext, ai_service) -> None:
        value = await ai_service.normalize_topic(message.text or "")
        await state.update_data(topic=value.model_dump(mode="json"))
        await state.set_state(TopicFlow.waiting_confirmation)
        keywords = "\n".join(f"• {escape(item)}" for item in value.keywords)
        await message.answer(
            f"<b>{escape(value.name)}</b>\n{escape(value.description)}\n\n{keywords}",
            reply_markup=topic_confirmation(),
            parse_mode="HTML",
        )

    @router.callback_query(F.data == "topic:save", TopicFlow.waiting_confirmation)
    async def topic_save(
        callback: CallbackQuery,
        state: FSMContext,
        confirmation_service,
        owner_user_id: int,
    ) -> None:
        from conference_planner.schemas.agent import AddTopicAction

        data = await state.get_data()
        topic = data["topic"]
        pending = await confirmation_service.propose(
            owner_user_id,
            AddTopicAction(
                name=topic["name"],
                description=topic.get("description", ""),
                keywords=tuple(topic.get("keywords", ())),
            ),
        )
        await state.clear()
        await callback.message.answer(pending.summary, reply_markup=action_confirmation(pending.id))
        await callback.answer()

    @router.callback_query(F.data.in_({"topic:cancel", "topic:edit"}))
    async def topic_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.data == "topic:edit":
            await state.set_state(TopicFlow.waiting_text)
            await callback.message.answer("Введите новую формулировку темы.")
        else:
            await state.clear()
            await callback.message.answer("Добавление отменено.")
        await callback.answer()

    @router.message(F.text == "Источники")
    async def sources(message: Message, state: FSMContext, source_repository) -> None:
        await open_menu(state)
        values = await source_repository.list_all()
        text = (
            "\n".join(
                f"{'✅' if item.enabled else '⏸'} {escape(source_display_name(item))} — "
                f"{escape(item.url)}"
                for item in values
            )
            or "Источников пока нет."
        )
        await message.answer(text, reply_markup=add_buttons("source"), parse_mode="HTML")

    @router.callback_query(F.data.startswith("source:toggle:"))
    async def source_toggle(callback: CallbackQuery, source_repository) -> None:
        _, _, raw_id, raw_enabled = callback.data.split(":")
        await source_repository.set_enabled(int(raw_id), raw_enabled == "1")
        await source_repository.session.commit()
        await callback.answer("Источник обновлён")

    @router.callback_query(F.data.startswith("source:delete:"))
    async def source_delete(callback: CallbackQuery, source_repository) -> None:
        await source_repository.delete(int(callback.data.rsplit(":", 1)[1]))
        await source_repository.session.commit()
        await callback.answer("Источник удалён")

    @router.callback_query(F.data == "source:add")
    async def source_add(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SourceFlow.waiting_value)
        await callback.message.answer(
            "Введите домен (например, iau.org) или полную ссылку на страницу мероприятий."
        )
        await callback.answer()

    @router.message(SourceFlow.waiting_value, F.text.func(is_flow_text))
    async def source_value(
        message: Message,
        state: FSMContext,
        confirmation_service,
        owner_user_id: int,
    ) -> None:
        from conference_planner.schemas.agent import AddSourceAction

        parsed = parse_source_input(message.text or "")
        if parsed is None:
            await message.answer(
                "Введите домен (example.org) или ссылку на страницу (https://example.org/events)."
            )
            return
        source_type, value = parsed
        pending = await confirmation_service.propose(
            owner_user_id,
            AddSourceAction(source_type=source_type, value=value),
        )
        await state.clear()
        await message.answer(pending.summary, reply_markup=action_confirmation(pending.id))

    @router.callback_query(F.data.startswith("conference:track:"))
    async def track(callback: CallbackQuery, confirmation_service, owner_user_id: int) -> None:
        from conference_planner.schemas.agent import TrackConferenceAction

        pending = await confirmation_service.propose(
            owner_user_id,
            TrackConferenceAction(conference_id=int(callback.data.rsplit(":", 1)[1])),
        )
        await callback.message.answer(pending.summary, reply_markup=action_confirmation(pending.id))
        await callback.answer()

    @router.callback_query(F.data.startswith("conference:archive:"))
    async def archive(callback: CallbackQuery, confirmation_service, owner_user_id: int) -> None:
        from conference_planner.schemas.agent import ArchiveConferenceAction

        pending = await confirmation_service.propose(
            owner_user_id,
            ArchiveConferenceAction(conference_id=int(callback.data.rsplit(":", 1)[1])),
        )
        await callback.message.answer(pending.summary, reply_markup=action_confirmation(pending.id))
        await callback.answer()

    router.include_router(build_conference_router())
    router.include_router(build_usage_router())
    router.include_router(build_model_router())
    router.include_router(build_confirmation_router())
    router.include_router(build_test_reminder_router())
    router.include_router(build_agent_router())

    fallback = Router(name="callback-fallback")

    @fallback.callback_query()
    async def callback_fallback(callback: CallbackQuery) -> None:
        await callback.answer("Действие больше не актуально.")

    router.include_router(fallback)

    return router


def is_flow_text(text: str | None) -> bool:
    value = (text or "").strip()
    return (
        bool(value)
        and not value.startswith("/")
        and value not in MAIN_MENU_BUTTONS
        and not is_next_candidate_text(value)
    )


def is_next_candidate_text(text: str | None) -> bool:
    return (text or "").strip().casefold() in NEXT_CANDIDATE_TEXTS


def parse_source_input(text: str) -> tuple[SourceType, str] | None:
    value = text.strip()
    if not value:
        return None
    kind, separator, explicit_value = value.partition(" ")
    if separator and kind.upper() in SourceType.__members__ and explicit_value.strip():
        return SourceType[kind.upper()], explicit_value.strip()
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return SourceType.PAGE, value
    if " " not in value and "." in value and "/" not in value:
        return SourceType.DOMAIN, value
    return None


async def open_menu(state: FSMContext) -> None:
    await state.clear()


async def cancel_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Текущее действие отменено.", reply_markup=main_menu())
