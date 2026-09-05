from datetime import date

from conference_scout.bot.messages import deadline_label, format_days_left, format_verified


def test_deadline_labels_and_verification_are_user_friendly() -> None:
    assert deadline_label("ABSTRACT_SUBMISSION") == "Подача тезисов"
    assert format_verified(True) == "✅ подтверждено официальным источником"
    assert format_verified(False) == "⚠️ найдено на вторичном источнике"


def test_days_left_is_computed_in_python() -> None:
    assert format_days_left(date(2027, 2, 12), date(2027, 2, 1)) == "через 11 дней"
    assert format_days_left(date(2027, 2, 1), date(2027, 2, 1)) == "сегодня"
