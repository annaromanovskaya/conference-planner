import json
from collections.abc import Sequence
from typing import Any

import httpx

from conference_planner.ai.gateway import ModelGatewayError
from conference_planner.ai.models import (
    AvailableModel,
    ChatMessage,
    ChatResponse,
    ToolCall,
    ToolDefinition,
)


class OpenAICompatibleGateway:
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
        payload = await self._request("GET", "/models")
        try:
            return tuple(AvailableModel(name=item["id"]) for item in payload["data"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError("Model service returned an invalid model list") from exc

    async def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": [self._message_payload(message) for message in messages],
        }
        if tools:
            body["tools"] = [self._tool_payload(tool) for tool in tools]
            body["tool_choice"] = "auto"
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        payload = await self._request("POST", "/chat/completions", json=body)
        try:
            message = payload["choices"][0]["message"]
            calls = tuple(self._parse_tool_call(item) for item in message.get("tool_calls", ()))
            content = message.get("content")
            if content is None and not calls:
                raise KeyError("content")
            return ChatResponse(content=content or "", tool_calls=calls)
        except (IndexError, KeyError, TypeError, ValueError) as exc:
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
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
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

    @staticmethod
    def _parse_tool_call(item: dict[str, Any]) -> ToolCall:
        raw_arguments = item["function"]["arguments"]
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        return ToolCall(
            id=item.get("id"),
            name=item["function"]["name"],
            arguments=arguments,
        )
