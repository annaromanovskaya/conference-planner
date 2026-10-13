import pytest
from pydantic import ValidationError

from conference_scout.config import Settings


@pytest.fixture
def valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")


def test_settings_parse_reminder_days(valid_environment: None) -> None:
    settings = Settings(_env_file=None)

    assert settings.reminder_days == (30, 14, 7, 3, 1)


def test_settings_reject_invalid_relevance(
    valid_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIN_RELEVANCE_SCORE", "1.1")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_reject_empty_reminders(
    valid_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REMINDER_DAYS", "")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_do_not_require_openrouter(valid_environment: None) -> None:
    settings = Settings(_env_file=None)

    assert settings.ollama_cloud_api_key is None
    assert settings.agent_max_steps == 6
    assert settings.agent_max_tool_calls_per_step == 3
    assert settings.agent_timeout_seconds == 90
    assert settings.mcp_host == "127.0.0.1"
    assert settings.mcp_port == 8765


def test_settings_default_cloud_allowlist(valid_environment: None) -> None:
    settings = Settings(_env_file=None)

    assert settings.ollama_cloud_free_models == (
        "gemma4:31b",
        "gpt-oss:120b",
        "gpt-oss:20b",
        "nemotron-3-nano:30b",
        "nemotron-3-super",
        "nemotron-3-ultra",
    )


def test_settings_parse_cloud_allowlist(
    valid_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_CLOUD_FREE_MODELS", "gpt-oss:20b, gemma4:31b")

    settings = Settings(_env_file=None)

    assert settings.ollama_cloud_free_models == ("gpt-oss:20b", "gemma4:31b")


def test_settings_reject_empty_cloud_allowlist(
    valid_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_CLOUD_FREE_MODELS", "")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_keep_model_keys_secret(
    valid_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "cloud-secret")
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "local-secret")

    settings = Settings(_env_file=None)

    assert settings.ollama_cloud_api_key is not None
    assert settings.ollama_cloud_api_key.get_secret_value() == "cloud-secret"
    assert str(settings.ollama_cloud_api_key) == "**********"
    assert settings.local_openai_api_key is not None
    assert str(settings.local_openai_api_key) == "**********"
