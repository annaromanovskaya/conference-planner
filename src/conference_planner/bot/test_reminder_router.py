from datetime import date, datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from conference_planner.bot.keyboards import (
    main_menu,
    test_reminder_confirmation,
    test_reminder_deadline_types,
)
from conference_planner.bot.states import TestReminderFlow
from conference_planner.db.repositories.test_reminders import TestReminderRepository
from conference_planner.reminders.test_service import (
    TestReminderDraft,
    TestReminderService,
    parse_test_send_at,
)
from conference_planner.runtime_clock import ApplicationClock
from conference_planner.schemas.common import DeadlineType

_DEADLINE_TYPE_LABELS = {
    DeadlineType.ABSTRACT_SUBMISSION: "Подача абстрактов",
    DeadlineType.ORAL_ABSTRACT_SUBMISSION: "Подача абстрактов для устных докладов",
    DeadlineType.REGISTRATION: "Регистрация",
    DeadlineType.REGISTRATION_OPEN: "Открытие регистрации",
    DeadlineType.EARLY_REGISTRATION: "Ранняя регистрация",
    DeadlineType.ACCEPTANCE_NOTIFICATION: "Уведомление о принятии",
    DeadlineType.POSTER_SUBMISSION: "Подача постера",
    DeadlineType.PAYMENT: "Оплата",
    DeadlineType.OTHER: "Другой дедлайн",
}


def build_test_reminder_router() -> Router:
    router = Router(name="test-reminders")
    router.message.register(start_test_reminder, Command("test_reminder"))
    router.message.register(cancel_test_reminder, Command("cancel_test_reminder"))
    router.message.register(
        receive_name, TestReminderFlow.waiting_name, F.text.func(_is_dialog_text)
    )
    router.callback_query.register(
        choose_deadline_type,
        TestReminderFlow.waiting_type,
        F.data.startswith("test-reminder:type:"),
    )
    router.message.register(
        receive_deadline_date,
        TestReminderFlow.waiting_deadline,
        F.text.func(_is_dialog_text),
    )
    router.message.register(
        receive_send_at,
        TestReminderFlow.waiting_send_at,
        F.text.func(_is_dialog_text),
    )
    router.callback_query.register(
        confirm_test_reminder,
        TestReminderFlow.waiting_confirmation,
        F.data == "test-reminder:confirm",
    )
    router.callback_query.register(
        cancel_test_reminder_flow,
        TestReminderFlow.waiting_confirmation,
        F.data == "test-reminder:cancel",
    )
    return router


async def start_test_reminder(
    message: Message,
    state: FSMContext,
    test_reminder_repository: TestReminderRepository,
    owner_user_id: int,
) -> None:
    await state.clear()
    active = await test_reminder_repository.active_for(owner_user_id)
    if active is not None:
        await message.answer(
            "У вас уже есть тестовое напоминание. Удалите его командой "
            "/cancel_test_reminder, затем создайте новое."
        )
        return
    await state.set_state(TestReminderFlow.waiting_name)
    await message.answer("Введите название тестовой конференции.")


async def receive_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 200:
        await message.answer("Введите название длиной от 1 до 200 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(TestReminderFlow.waiting_type)
    await message.answer(
        "Выберите тип дедлайна.", reply_markup=test_reminder_deadline_types()
    )


async def choose_deadline_type(callback: CallbackQuery, state: FSMContext) -> None:
    raw_type = (callback.data or "").rsplit(":", 1)[-1]
    try:
        deadline_type = DeadlineType(raw_type)
    except ValueError:
        await callback.answer("Неизвестный тип дедлайна.", show_alert=True)
        return
    await state.update_data(deadline_type=deadline_type.value)
    await state.set_state(TestReminderFlow.waiting_deadline)
    await callback.message.answer("Введите дату дедлайна в формате ДД.ММ.ГГГГ.")
    await callback.answer()


async def receive_deadline_date(
    message: Message, state: FSMContext, application_clock: ApplicationClock
) -> None:
    try:
        deadline = datetime.strptime((message.text or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Введите существующую дату в формате ДД.ММ.ГГГГ.")
        return
    if deadline < application_clock.today():
        await message.answer("Дата дедлайна уже прошла. Введите будущую дату.")
        return
    await state.update_data(deadline_date=deadline.isoformat())
    await state.set_state(TestReminderFlow.waiting_send_at)
    await message.answer(
        "Когда отправить тест? Введите ДД.ММ.ГГГГ ЧЧ:ММ или, например, "
        "«через 10 минут», «через 2 часа», «через 3 дня»."
    )


async def receive_send_at(
    message: Message, state: FSMContext, application_clock: ApplicationClock
) -> None:
    try:
        send_at = parse_test_send_at(
            message.text or "",
            now=application_clock.now(),
            timezone=application_clock.timezone,
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    deadline = date.fromisoformat(data["deadline_date"])
    local_send_at = send_at.astimezone(application_clock.timezone)
    if local_send_at.date() > deadline:
        await message.answer("Время отправки не может быть позже дня дедлайна.")
        return
    await state.update_data(send_at=send_at.isoformat())
    await state.set_state(TestReminderFlow.waiting_confirmation)
    deadline_type = DeadlineType(data["deadline_type"])
    await message.answer(
        "<b>ТЕСТОВОЕ НАПОМИНАНИЕ</b>\n\n"
        f"Название: {escape(data['name'])}\n"
        f"Тип: {escape(_DEADLINE_TYPE_LABELS[deadline_type])}\n"
        f"Дата дедлайна: {deadline:%d.%m.%Y}\n"
        f"Отправка: {local_send_at:%d.%m.%Y %H:%M} "
        f"({escape(str(application_clock.timezone))})",
        reply_markup=test_reminder_confirmation(),
        parse_mode="HTML",
    )


async def confirm_test_reminder(
    callback: CallbackQuery,
    state: FSMContext,
    test_reminder_repository: TestReminderRepository,
    application_clock: ApplicationClock,
    owner_user_id: int,
) -> None:
    data = await state.get_data()
    try:
        draft = TestReminderDraft(
            name=data["name"],
            deadline_type=DeadlineType(data["deadline_type"]),
            deadline_date=date.fromisoformat(data["deadline_date"]),
            send_at=datetime.fromisoformat(data["send_at"]),
        )
        await TestReminderService(
            test_reminder_repository, application_clock.timezone
        ).schedule(owner_user_id, draft, now=application_clock.now())
    except (KeyError, ValueError) as exc:
        await callback.answer(str(exc) or "Не удалось создать напоминание.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer(
        "Тестовое напоминание создано. После успешной отправки тестовая "
        "конференция и её дедлайн удалятся автоматически.",
        reply_markup=main_menu(),
    )
    await callback.answer()


async def cancel_test_reminder_flow(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.clear()
    await callback.message.answer("Создание тестового напоминания отменено.")
    await callback.answer()


async def cancel_test_reminder(
    message: Message,
    state: FSMContext,
    test_reminder_repository: TestReminderRepository,
    application_clock: ApplicationClock,
    owner_user_id: int,
) -> None:
    await state.clear()
    removed = await TestReminderService(
        test_reminder_repository, application_clock.timezone
    ).cancel(owner_user_id)
    text = (
        "Тестовое напоминание удалено."
        if removed
        else "Активного тестового напоминания нет."
    )
    await message.answer(text, reply_markup=main_menu())


def _is_dialog_text(value: str | None) -> bool:
    text = (value or "").strip()
    return bool(text) and not text.startswith("/")
