from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandDescription:
    usage: str
    description: str


COMMANDS = (
    CommandDescription("/start", "открыть главное меню"),
    CommandDescription("/help", "показать возможности и список команд"),
    CommandDescription("/model", "выбрать подключение и модель"),
    CommandDescription("/new", "начать новый диалог, сохранив долгосрочную память"),
    CommandDescription("/memory", "показать долгосрочную память"),
    CommandDescription("/forget ID", "предложить удалить запись памяти с указанным ID"),
    CommandDescription("/cancel", "отменить текущее действие или ввод"),
    CommandDescription("/test_reminder", "создать одноразовое тестовое напоминание"),
    CommandDescription(
        "/cancel_test_reminder", "удалить активное тестовое напоминание"
    ),
)

UNKNOWN_COMMAND_TEXT = (
    "Неизвестная команда. Посмотрите список доступных команд через /help "
    "или раздел «Настройки»."
)


def command_help_text(*, title: str = "КОМАНДЫ") -> str:
    lines = [title]
    lines.extend(f"{item.usage} — {item.description}" for item in COMMANDS)
    return "\n".join(lines)
