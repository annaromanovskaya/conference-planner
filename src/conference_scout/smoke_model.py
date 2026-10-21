import argparse
import asyncio
from collections.abc import Callable

import httpx

from conference_scout.ai.models import ChatMessage, ToolDefinition
from conference_scout.ai.registry import build_provider_profiles
from conference_scout.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Opt-in model and tool-calling smoke check")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--model", required=True)
    return parser


async def run_probe(gateway, model: str, output: Callable[[str], None] = print) -> int:
    try:
        available = {item.name for item in await gateway.list_models()}
        if model not in available:
            output("ERROR: указанная модель сейчас отсутствует в API.")
            return 1
        probe = ToolDefinition(
            name="capability_probe",
            description="Return the harmless supplied value.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        response = await gateway.chat(
            model,
            [
                ChatMessage(role="system", content="Call capability_probe."),
                ChatMessage(role="user", content="Call it with value ok."),
            ],
            [probe],
        )
        supports_tools = any(call.name == probe.name for call in response.tool_calls)
        mode = "вызов инструментов поддерживается" if supports_tools else "режим только чата"
        output(f"OK: {model} доступна; {mode}.")
        return 0
    except Exception:
        output("ERROR: проверка модели не выполнена; проверьте endpoint, ключ и лимиты.")
        return 1


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        profiles = {profile.id: profile for profile in build_provider_profiles(settings, client)}
        profile = profiles.get(args.profile)
        if profile is None:
            print("ERROR: профиль не настроен.")
            return 1
        if profile.allowed_models is not None and args.model not in profile.allowed_models:
            print("ERROR: модель не входит в настроенный Cloud allowlist.")
            return 1
        return await run_probe(profile.gateway, args.model)


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))
