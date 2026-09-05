import pytest
from pydantic import ValidationError

from conference_scout.config import Settings


@pytest.fixture
def valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")


def test_settings_parse_reminder_days(valid_environment: None) -> None:
    settings = Settings()

    assert settings.reminder_days == (30, 14, 7, 3, 1)


def test_settings_reject_invalid_relevance(
    valid_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIN_RELEVANCE_SCORE", "1.1")

    with pytest.raises(ValidationError):
        Settings()


def test_settings_reject_empty_reminders(
    valid_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REMINDER_DAYS", "")

    with pytest.raises(ValidationError):
        Settings()
