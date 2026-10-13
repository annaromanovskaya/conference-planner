from collections.abc import Sequence
from typing import Any

import httpx

from conference_scout.ai.gateway import ModelGatewayError
from conference_scout.ai.models import (
    AvailableModel,
    ChatMessage,
    ChatResponse,
    ToolCall,
    ToolDefinition,
)


class OllamaGateway:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str | None = None,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def list_models(self) -> Sequence[AvailableModel]:
        payload = await self._request("GET", "/api/tags")
        try:
            models = payload["models"]
            return tuple(AvailableModel(name=item["name"]) for item in models)
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError("Model service returned an invalid model list") from exc

    async def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": [self._message_payload(message) for message in messages],
            "stream": False,
        }
        if tools:
            body["tools"] = [self._tool_payload(tool) for tool in tools]
        payload = await self._request("POST", "/api/chat", json=body)
        try:
            message = payload["message"]
            raw_calls = message.get("tool_calls", ())
            calls = tuple(
                ToolCall(
                    id=item.get("id"),
                    name=item["function"]["name"],
                    arguments=item["function"]["arguments"],
                )
                for item in raw_calls
            )
            if "content" not in message and not calls:
                raise KeyError("content")
            return ChatResponse(content=message.get("content", ""), tool_calls=calls)
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError("Model service returned an invalid chat response") from exc

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method, f"{self.base_url}{path}", headers=self.headers, **kwargs
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("response must be an object")
            return payload
        except httpx.HTTPStatusError as exc:
            code = "authentication" if exc.response.status_code in {401, 403} else "upstream_error"
            raise ModelGatewayError("Model service request failed", code=code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelGatewayError("Model service request failed") from exc

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ]
        if message.tool_name:
            payload["tool_name"] = message.tool_name
        return payload

    @staticmethod
    def _tool_payload(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
