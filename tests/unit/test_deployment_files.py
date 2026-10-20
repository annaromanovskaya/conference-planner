import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_compose_hardens_application_and_keeps_searxng_internal() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    app = compose["services"]["confplanner"]
    search = compose["services"]["searxng"]
    assert app["read_only"] is True
    assert "/tmp" in app["tmpfs"]
    assert app["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in app["security_opt"]
    assert app["restart"] == "unless-stopped"
    assert "ports" not in app
    assert "ports" not in search
    assert "extra_hosts" not in app
    assert all("docker.sock" not in volume for volume in app["volumes"])


def test_dockerfile_runs_as_non_root() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "mkdir -p /data" in dockerfile
    assert "chown conference-scout:conference-scout /data" in dockerfile
    assert "USER conference-scout" in dockerfile
    assert "conference-scout-healthcheck" in dockerfile
    assert "COPY migrations /app/migrations" in dockerfile


def test_example_environment_has_model_profiles_but_no_credentials() -> None:
    example = (ROOT / ".env.example").read_text()
    assert "OLLAMA_CLOUD_API_KEY=YOUR_OLLAMA_CLOUD_API_KEY" in example
    assert "LLM_HUB_BASE_URL=" in example
    assert "LLM_HUB_API_KEY=" in example
    assert "OLLAMA_LOCAL_" not in example
    assert "LOCAL_OPENAI_" not in example
    assert "OPENROUTER" not in example
    assert "hub_" not in example
    assert "APP_TIMEZONE=Europe/Moscow" in example
    assert "DISCOVERY_DAY=" not in example
    assert "DISCOVERY_HOUR=10" in example
    assert "REMINDER_HOUR=9" in example
    assert "REMINDER_DAYS=30,14,7,3,1,0" in example


def test_runtime_installs_sqlalchemy_asyncio_extra() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert "sqlalchemy[asyncio]>=2.0.41,<3" in project["project"]["dependencies"]
