import json
import os
import logging
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


logger = logging.getLogger(__name__)


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
            raw_model_name = payload.get("model")
            model_name = (
                raw_model_name.strip()
                if isinstance(raw_model_name, str) and raw_model_name.strip()
                else None
            )
            if model_name == model == "auto":
                model_name = None
            return ChatResponse(
                content=content or "",
                tool_calls=calls,
                model_name=model_name,
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError("Model service returned an invalid chat response") from exc

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            # Large scientific programme pages may need far more than the
            # short timeout used by ordinary website fetches.
            timeout_seconds = max(20.0, float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "120")))
            response = await self.client.request(
                method, f"{self.base_url}{path}", headers=self.headers,
                timeout=httpx.Timeout(connect=10.0, read=timeout_seconds, write=20.0, pool=20.0),
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("response must be an object")
            return payload
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            code = "authentication" if status in {401, 403} else f"http_{status}"
            logger.warning("Local model API returned HTTP %d for %s", status, path)
            raise ModelGatewayError(f"Model service HTTP {status}", code=code) from exc
        except httpx.TimeoutException as exc:
            logger.warning("Model API timeout during %s: %s", path, type(exc).__name__)
            raise ModelGatewayError(
                f"Model service timeout ({type(exc).__name__})", code="timeout"
            ) from exc
        except httpx.RequestError as exc:
            logger.warning("Model API transport failure during %s: %s", path, type(exc).__name__)
            raise ModelGatewayError(
                f"Model service transport error ({type(exc).__name__})", code="transport_error"
            ) from exc
        except ValueError as exc:
            raise ModelGatewayError("Model service returned malformed JSON", code="invalid_json") from exc

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
