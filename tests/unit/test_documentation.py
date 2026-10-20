from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_readme_contains_required_operator_sections() -> None:
    readme = (ROOT / "README.md").read_text()
    for heading in (
        "Features",
        "Architecture",
        "How it works",
        "Requirements",
        "Quick start",
        "Telegram bot creation",
        "Model configuration",
        "Telegram commands",
        "Memory and confirmations",
        "Model smoke check",
        "SearXNG",
        "Configuration",
        "Docker deployment",
        "Trusted sources",
        "Topics",
        "Discovery",
        "Deadline tracking",
        "Backups",
        "Security",
        "Development",
        "Testing",
        "Limitations",
        "Roadmap",
        "License",
    ):
        assert f"## {heading}" in readme
    assert "docker compose up -d" in readme
    assert "/data/conference_scout.db" in readme
    assert "LLM_HUB_BASE_URL" in readme
    assert "Free usage credits" in readme
    assert "ordinary text" in readme
    assert "MONITORING_INTERVAL_HOURS" in readme
    assert "AGENT_RESEARCH_MODEL" in readme
    assert "AGENT_VERIFIER_MODEL" in readme
    assert "AGENT_WORKFLOWS_ENABLED" in readme
    assert "72 часа" in readme
    assert "сохраняемый workflow" in readme
    assert "### Локальная дата и часовой пояс" in readme
    assert "### Снимок проверки кандидата" in readme
    assert "### Ручные дедлайны" in readme
    assert "### Двухступенчатая проверка релевантности" in readme
    assert "APP_TIMEZONE=Europe/Moscow" in readme
    assert "каждый понедельник" in readme
    assert "Отслеживать" in readme
    assert "Пропустить" in readme
    assert "Отклонить" in readme
    assert "REMINDER_HOUR" in readme
    assert "30,14,7,3,1,0" in readme
    assert "раз в час" not in readme
    assert "OpenRouter" not in readme


def test_example_environment_contains_no_real_secrets() -> None:
    example = (ROOT / ".env.example").read_text()
    assert "YOUR_TELEGRAM_BOT_TOKEN" in example
    assert "YOUR_OLLAMA_CLOUD_API_KEY" in example
    assert "YOUR_LLM_HUB_API_KEY" in example
    assert "sk-or-" not in example
    assert "hub_" not in example
