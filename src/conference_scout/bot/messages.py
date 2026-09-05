from datetime import date

DEADLINE_LABELS = {
    "ABSTRACT_SUBMISSION": "Подача тезисов",
    "REGISTRATION": "Регистрация",
    "EARLY_REGISTRATION": "Ранняя регистрация",
    "OTHER": "Другой дедлайн",
}


def deadline_label(deadline_type: str) -> str:
    return DEADLINE_LABELS.get(deadline_type, "Дедлайн")


def format_verified(verified: bool) -> str:
    if verified:
        return "✅ подтверждено официальным источником"
    return "⚠️ найдено на вторичном источнике"


def format_days_left(deadline: date, today: date) -> str:
    days = (deadline - today).days
    if days == 0:
        return "сегодня"
    if days < 0:
        return "дедлайн прошёл"
    return f"через {days} дней"
