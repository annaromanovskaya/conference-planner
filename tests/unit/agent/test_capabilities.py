from conference_scout.agent.capabilities import (
    capability_help_text,
    is_capability_question,
)
from conference_scout.agent.prompts import BASE_SYSTEM_PROMPT


def test_capability_catalog_describes_real_automation_and_candidate_decisions() -> None:
    text = capability_help_text()

    assert "понедельник" in text.casefold()
    assert "отслеживать" in text.casefold()
    assert "пропустить" in text.casefold()
    assert "отклонить" in text.casefold()
    assert "раз в сутки" in text.casefold()


def test_only_explicit_capability_questions_are_recognized() -> None:
    assert is_capability_question("Что ты умеешь?") is True
    assert is_capability_question("Какие возможности у бота") is True
    assert is_capability_question("Расскажи, что может этот агент") is True
    assert is_capability_question("Что ты умеешь найти про IMBASE?") is False
    assert is_capability_question("Найди конференции") is False


def test_system_prompt_uses_the_same_capability_catalog() -> None:
    assert capability_help_text() in BASE_SYSTEM_PROMPT
