from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Мои темы"), KeyboardButton(text="Найти конференции")],
            [KeyboardButton(text="Конференции"), KeyboardButton(text="Ближайшие дедлайны")],
            [KeyboardButton(text="Источники"), KeyboardButton(text="Настройки")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Сообщение или команда для агента",
    )


def topic_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сохранить", callback_data="topic:save")],
            [InlineKeyboardButton(text="Изменить", callback_data="topic:edit")],
            [InlineKeyboardButton(text="Отмена", callback_data="topic:cancel")],
        ]
    )


def add_buttons(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Добавить", callback_data=f"{kind}:add")]]
    )


def action_confirmation(action_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить", callback_data=f"action:confirm:{action_id}"
                ),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"action:cancel:{action_id}"),
            ]
        ]
    )


def test_reminder_deadline_types() -> InlineKeyboardMarkup:
    values = (
        ("Подача абстрактов", "ABSTRACT_SUBMISSION"),
        ("Устный доклад", "ORAL_ABSTRACT_SUBMISSION"),
        ("Регистрация", "REGISTRATION"),
        ("Открытие регистрации", "REGISTRATION_OPEN"),
        ("Ранняя регистрация", "EARLY_REGISTRATION"),
        ("Постер", "POSTER_SUBMISSION"),
        ("Оплата", "PAYMENT"),
        ("Уведомление о принятии", "ACCEPTANCE_NOTIFICATION"),
        ("Другое", "OTHER"),
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"test-reminder:type:{value}"
                )
            ]
            for label, value in values
        ]
    )


def test_reminder_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Создать", callback_data="test-reminder:confirm"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data="test-reminder:cancel"
                ),
            ]
        ]
    )


def candidate_review_actions(candidate_id: int, source_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отслеживать", callback_data=f"candidate:track:{candidate_id}"
                ),
                InlineKeyboardButton(text="Подробнее", url=source_url),
            ],
            [
                InlineKeyboardButton(
                    text="Пропустить", callback_data=f"candidate:defer:{candidate_id}"
                ),
                InlineKeyboardButton(
                    text="Отклонить", callback_data=f"candidate:reject:{candidate_id}"
                ),
            ],
        ]
    )


def candidate_decision_actions(candidate_id: int, source_url: str) -> InlineKeyboardMarkup:
    return candidate_review_actions(candidate_id, source_url)


def conference_actions(
    conference_id: int,
    source_url: str | None = None,
    *,
    allow_check: bool = True,
) -> InlineKeyboardMarkup:
    if allow_check:
        rows = [
            [
                InlineKeyboardButton(
                    text="Проверить сейчас", callback_data=f"conference:check:{conference_id}"
                ),
                InlineKeyboardButton(
                    text="История", callback_data=f"conference:history:{conference_id}"
                ),
            ]
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(
                    text="История", callback_data=f"conference:history:{conference_id}"
                )
            ]
        ]
    if source_url:
        rows.append([InlineKeyboardButton(text="Открыть сайт", url=source_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def indexed_buttons(values: list[tuple[str, str]], *, prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{index}")]
            for index, (label, _value) in enumerate(values)
        ]
    )


def ollama_usage_keyboard(*, offer_model_choice: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Обновить статистику", callback_data="usage:refresh")],
        [
            InlineKeyboardButton(
                text="Точный Free usage в Ollama",
                url="https://ollama.com/settings/usage",
            )
        ],
    ]
    if offer_model_choice:
        rows.append(
            [InlineKeyboardButton(text="Выбрать другую модель", callback_data="usage:model")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
