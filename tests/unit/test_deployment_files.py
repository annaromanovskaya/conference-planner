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
    assert all("docker.sock" not in volume for volume in app["volumes"])


def test_dockerfile_runs_as_non_root() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "mkdir -p /data" in dockerfile
    assert "chown conference-scout:conference-scout /data" in dockerfile
    assert "USER conference-scout" in dockerfile
    assert "conference-scout-healthcheck" in dockerfile
