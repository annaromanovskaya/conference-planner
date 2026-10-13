import json

import httpx
import pytest

from conference_scout.ai.gateway import ModelGatewayError
from conference_scout.ai.models import ChatMessage, ToolDefinition
from conference_scout.ai.ollama import OllamaGateway


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_ollama_lists_models_with_cloud_authentication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://ollama.com/api/tags"
        assert request.headers["Authorization"] == "Bearer hub-secret"
        return httpx.Response(200, json={"models": [{"name": "gemma4:31b"}]})

    async with _client(handler) as client:
        models = await OllamaGateway(client, "https://ollama.com", "hub-secret").list_models()

    assert [model.name for model in models] == ["gemma4:31b"]


@pytest.mark.asyncio
async def test_ollama_sends_normalized_messages_and_tools() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://ollama.local:11434/api/chat"
        assert "Authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload == {
            "model": "gpt-oss:20b",
            "messages": [{"role": "user", "content": "nearest deadline"}],
            "stream": False,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "list_deadlines",
                        "description": "List upcoming deadlines",
                        "parameters": {
                            "type": "object",
                            "properties": {"days": {"type": "integer"}},
                            "required": ["days"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "list_deadlines",
                                "arguments": {"days": 30},
                            }
                        }
                    ],
                }
            },
        )

    tool = ToolDefinition(
        name="list_deadlines",
        description="List upcoming deadlines",
        input_schema={
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
            "additionalProperties": False,
        },
    )
    async with _client(handler) as client:
        response = await OllamaGateway(client, "http://ollama.local:11434").chat(
            "gpt-oss:20b", [ChatMessage(role="user", content="nearest deadline")], [tool]
        )

    assert response.content == ""
    assert response.tool_calls[0].name == "list_deadlines"
    assert response.tool_calls[0].arguments == {"days": 30}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"message": {"role": "assistant"}}),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(401, text="secret upstream body"),
    ],
)
async def test_ollama_failures_are_typed_and_secret_safe(response: httpx.Response) -> None:
    async with _client(lambda _request: response) as client:
        with pytest.raises(ModelGatewayError) as captured:
            await OllamaGateway(client, "https://ollama.com", "hub-secret").chat(
                "gemma4:31b", [ChatMessage(role="user", content="hello")]
            )

    error_text = str(captured.value)
    assert "hub-secret" not in error_text
    assert "secret upstream body" not in error_text
