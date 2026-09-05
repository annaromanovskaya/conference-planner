from html import escape

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from conference_scout.bot.access import OwnerOnlyMiddleware
from conference_scout.bot.errors import ExternalErrorMiddleware
from conference_scout.bot.keyboards import add_buttons, main_menu, topic_confirmation
from conference_scout.bot.states import SourceFlow, TopicFlow
from conference_scout.schemas.common import SourceType


def build_router(*, owner_telegram_id: int) -> Router:
    router = Router(name="owner")
    router.message.middleware(ExternalErrorMiddleware())
    router.callback_query.middleware(ExternalErrorMiddleware())
    router.message.middleware(OwnerOnlyMiddleware(owner_telegram_id))
    router.callback_query.middleware(OwnerOnlyMiddleware(owner_telegram_id))

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(
            "🔭 Conference Scout\n\nПерсональный поиск научных конференций.",
            reply_markup=main_menu(),
        )

    @router.message(F.text == "Мои темы")
    async def topics(message: Message, topic_repository) -> None:
        values = await topic_repository.list_for_user(topic_repository.owner_user_id)
        text = "\n".join(f"⭐ {escape(item.name)}" for item in values) or "Тем пока нет."
        await message.answer(text, reply_markup=add_buttons("topic"))

    @router.callback_query(F.data == "topic:add")
    async def topic_add(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(TopicFlow.waiting_text)
        await callback.message.answer("Опишите научную тематику свободным текстом.")
        await callback.answer()

    @router.message(TopicFlow.waiting_text)
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
    async def topic_save(callback: CallbackQuery, state: FSMContext, topic_repository) -> None:
        from conference_scout.schemas.topics import TopicNormalization

        data = await state.get_data()
        await topic_repository.create(
            topic_repository.owner_user_id, TopicNormalization.model_validate(data["topic"])
        )
        await topic_repository.session.commit()
        await state.clear()
        await callback.message.answer("✅ Тема сохранена.")
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
    async def sources(message: Message, source_repository) -> None:
        values = await source_repository.list_all()
        text = (
            "\n".join(
                f"{'✅' if item.enabled else '⏸'} {escape(item.name)} — {escape(item.url)}"
                for item in values
            )
            or "Источников пока нет."
        )
        await message.answer(text, reply_markup=add_buttons("source"))

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
            "Введите DOMAIN example.org или PAGE https://example.org/events"
        )
        await callback.answer()

    @router.message(SourceFlow.waiting_value)
    async def source_value(message: Message, state: FSMContext, source_repository) -> None:
        kind, separator, value = (message.text or "").strip().partition(" ")
        if not separator or kind.upper() not in SourceType.__members__ or not value.strip():
            await message.answer("Формат: DOMAIN example.org или PAGE https://example.org/events")
            return
        source_type = SourceType[kind.upper()]
        await source_repository.create(value.strip(), value.strip(), source_type)
        await source_repository.session.commit()
        await state.clear()
        await message.answer("✅ Источник сохранён.")

    @router.message(F.text == "Найти конференции")
    async def discover(message: Message, discovery_runner) -> None:
        await message.answer("🔎 Поиск запущен. Результат будет отправлен после завершения.")
        report = await discovery_runner()
        await message.answer(f"Найдено новых конференций: {len(report.created_ids)}")

    @router.message(F.text == "Конференции")
    async def conferences(message: Message, conference_repository) -> None:
        from conference_scout.schemas.common import ConferenceStatus

        blocks: list[str] = []
        for status, title in (
            (ConferenceStatus.TRACKING, "Отслеживаемые"),
            (ConferenceStatus.DISCOVERED, "Найденные"),
            (ConferenceStatus.ARCHIVED, "Архив"),
        ):
            values = await conference_repository.list_by_status(status)
            lines = "\n".join(f"• {escape(item.name)} (ID {item.id})" for item in values) or "—"
            blocks.append(f"<b>{title}</b>\n{lines}")
        await message.answer("\n\n".join(blocks), parse_mode="HTML")

    @router.callback_query(F.data.startswith("conference:track:"))
    async def track(callback: CallbackQuery, conference_repository) -> None:
        await conference_repository.track(
            conference_repository.owner_user_id, int(callback.data.rsplit(":", 1)[1])
        )
        await conference_repository.session.commit()
        await callback.answer("Конференция отслеживается")

    @router.callback_query(F.data.startswith("conference:archive:"))
    async def archive(callback: CallbackQuery, conference_repository) -> None:
        await conference_repository.archive(int(callback.data.rsplit(":", 1)[1]))
        await conference_repository.session.commit()
        await callback.answer("Конференция перемещена в архив")

    @router.message(F.text == "Ближайшие дедлайны")
    async def deadlines(message: Message, reminder_repository) -> None:
        values = await reminder_repository.candidates(reminder_repository.owner_user_id)
        values.sort(key=lambda item: item.deadline_date)
        text = (
            "\n".join(
                f"• {escape(item.conference_name)} — {item.deadline_date.isoformat()}"
                for item in values
            )
            or "Будущих дедлайнов пока нет."
        )
        await message.answer(text)

    @router.message(F.text == "Настройки")
    async def settings(message: Message) -> None:
        await message.answer("Настройки расписания и модели задаются владельцем через .env.")

    @router.callback_query()
    async def callback_fallback(callback: CallbackQuery) -> None:
        await callback.answer("Действие больше не актуально.")

    return router
