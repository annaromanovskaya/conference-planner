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
        "OpenRouter configuration",
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


def test_example_environment_contains_no_real_secrets() -> None:
    example = (ROOT / ".env.example").read_text()
    assert "YOUR_TELEGRAM_BOT_TOKEN" in example
    assert "YOUR_OLLAMA_CLOUD_API_KEY" in example
    assert "sk-or-" not in example
    assert "hub_" not in example
