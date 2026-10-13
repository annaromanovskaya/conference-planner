import pytest

from conference_scout.ai.gateway import ModelGatewayError
from conference_scout.ai.models import AvailableModel, ChatResponse, ToolCall
from conference_scout.smoke_model import build_parser, run_probe


def test_smoke_arguments_are_required() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


@pytest.mark.asyncio
async def test_smoke_probe_reports_capability_with_fake_gateway() -> None:
    class Gateway:
        async def list_models(self):
            return (AvailableModel(name="model"),)

        async def chat(self, model, messages, tools=()):
            assert model == "model" and tools
            return ChatResponse(
                tool_calls=(ToolCall(name="capability_probe", arguments={"value": "ok"}),)
            )

    output = []
    code = await run_probe(Gateway(), "model", output.append)

    assert code == 0
    assert output == ["OK: model доступна; вызов инструментов поддерживается."]


@pytest.mark.asyncio
async def test_smoke_failure_never_prints_provider_details() -> None:
    class Gateway:
        async def list_models(self):
            raise ModelGatewayError("hub_secret response body")

    output = []
    code = await run_probe(Gateway(), "model", output.append)

    assert code == 1
    assert "hub_secret" not in output[0]
