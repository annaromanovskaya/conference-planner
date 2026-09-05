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
