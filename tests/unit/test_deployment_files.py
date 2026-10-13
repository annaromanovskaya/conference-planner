from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_compose_hardens_application_and_keeps_searxng_internal() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    app = compose["services"]["conference-scout"]
    search = compose["services"]["searxng"]
    assert app["read_only"] is True
    assert "/tmp" in app["tmpfs"]
    assert app["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in app["security_opt"]
    assert app["restart"] == "unless-stopped"
    assert "ports" not in app
    assert "ports" not in search
    assert "host.docker.internal:host-gateway" in app["extra_hosts"]
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
    assert "OLLAMA_LOCAL_BASE_URL=http://host.docker.internal:11434" in example
    assert "LOCAL_OPENAI_BASE_URL=" in example
    assert "OPENROUTER" not in example
    assert "hub_" not in example
