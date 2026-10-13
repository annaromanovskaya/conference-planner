import json

import httpx
import pytest

from conference_scout.ai.gateway import ModelGatewayError
from conference_scout.ai.models import ChatMessage, ToolDefinition
from conference_scout.ai.openai_compatible import OpenAICompatibleGateway


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_openai_compatible_lists_models_with_optional_authentication() -> None:
    seen_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://models.local/v1/models"
        seen_auth.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"data": [{"id": "local-agent"}]})

    async with _client(handler) as client:
        no_key = await OpenAICompatibleGateway(client, "http://models.local/v1").list_models()
        with_key = await OpenAICompatibleGateway(
            client, "http://models.local/v1/", "local-secret"
        ).list_models()

    assert [item.name for item in no_key] == ["local-agent"]
    assert [item.name for item in with_key] == ["local-agent"]
    assert seen_auth == [None, "Bearer local-secret"]


@pytest.mark.asyncio
async def test_openai_compatible_parses_plain_chat_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload == {
            "model": "local-agent",
            "messages": [{"role": "user", "content": "hello"}],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]},
        )

    async with _client(handler) as client:
        response = await OpenAICompatibleGateway(client, "http://models.local/v1").chat(
            "local-agent", [ChatMessage(role="user", content="hello")]
        )

    assert response.content == "Hello!"
    assert response.tool_calls == ()


@pytest.mark.asyncio
async def test_openai_compatible_sends_and_parses_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tools"][0]["function"]["name"] == "list_topics"
        assert payload["tool_choice"] == "auto"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "list_topics",
                                        "arguments": '{"enabled":true}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    tool = ToolDefinition(
        name="list_topics",
        description="List topics",
        input_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "additionalProperties": False,
        },
    )
    async with _client(handler) as client:
        response = await OpenAICompatibleGateway(client, "http://models.local/v1").chat(
            "local-agent", [ChatMessage(role="user", content="topics")], [tool]
        )

    assert response.content == ""
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].name == "list_topics"
    assert response.tool_calls[0].arguments == {"enabled": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"choices": []}),
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {"id": "bad", "function": {"name": "x", "arguments": "{"}}
                            ],
                        }
                    }
                ]
            },
        ),
        httpx.Response(500, text="local-secret and private body"),
    ],
)
async def test_openai_compatible_failures_are_typed_and_secret_safe(
    response: httpx.Response,
) -> None:
    async with _client(lambda _request: response) as client:
        with pytest.raises(ModelGatewayError) as captured:
            await OpenAICompatibleGateway(client, "http://models.local/v1", "local-secret").chat(
                "local-agent", [ChatMessage(role="user", content="hello")]
            )

    error_text = str(captured.value)
    assert "local-secret" not in error_text
    assert "private body" not in error_text
