import asyncio
import json
from collections.abc import Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaError
from pydantic import BaseModel, ConfigDict, ValidationError

from conference_scout.agent.memory import MemoryService
from conference_scout.agent.prompts import build_system_prompt
from conference_scout.ai.models import ChatMessage, ToolCall, ToolDefinition
from conference_scout.ai.registry import ModelRegistry
from conference_scout.mcp.client import ToolClient, ToolResult
from conference_scout.schemas.agent import DomainReference, PendingActionView
from conference_scout.schemas.common import PendingActionStatus


class AgentReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    pending_actions: tuple[PendingActionView, ...] = ()
    references: tuple[DomainReference, ...] = ()
    error_code: str | None = None


class _RuntimeFailure(RuntimeError):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(code)
        self.code = code
        self.text = text


class AgentRuntime:
    def __init__(
        self,
        registry: ModelRegistry,
        memory: MemoryService,
        tools: ToolClient,
        *,
        max_steps: int,
        max_tool_calls_per_step: int,
        timeout_seconds: float,
        max_tool_result_chars: int,
    ) -> None:
        self.registry = registry
        self.memory = memory
        self.tools = tools
        self.max_steps = max_steps
        self.max_tool_calls_per_step = max_tool_calls_per_step
        self.timeout_seconds = timeout_seconds
        self.max_tool_result_chars = max_tool_result_chars

    async def respond(self, user_id: int, text: str) -> AgentReply:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._respond(user_id, text)
        except TimeoutError:
            await asyncio.shield(self.memory.session.rollback())
            return AgentReply(
                text="Превышено время ожидания ответа. Попробуйте ещё раз.",
                error_code="timeout",
            )
        except _RuntimeFailure as exc:
            return await self._safe_reply(user_id, exc.text, exc.code)
        except Exception:
            return await self._safe_reply(
                user_id,
                "Не удалось обработать запрос. Попробуйте ещё раз.",
                "agent_failure",
            )

    async def _respond(self, user_id: int, text: str) -> AgentReply:
        context = await self.memory.get_context(user_id, text)
        await self.memory.append_message(user_id, "user", text)
        try:
            active = await self.registry.get_active(user_id)
            gateway = self.registry.gateway_for(active.profile_id)
        except Exception as exc:
            raise _RuntimeFailure(
                "provider_failure", "Модель сейчас недоступна. Проверьте выбор через /model."
            ) from exc

        tools: tuple[ToolDefinition, ...] = ()
        if active.supports_tools is True:
            try:
                tools = tuple(await self.tools.list_tools())
            except Exception as exc:
                raise _RuntimeFailure(
                    "tool_failure", "Инструменты конференций сейчас недоступны."
                ) from exc

        messages = [
            ChatMessage(
                role="system",
                content=build_system_prompt(context, tools_available=bool(tools)),
            ),
            *(ChatMessage(role=item.role, content=item.content) for item in context.messages),
            ChatMessage(role="user", content=text),
        ]
        definitions = {tool.name: tool for tool in tools}
        references: list[DomainReference] = []
        pending: list[PendingActionView] = []

        for _step in range(self.max_steps):
            try:
                response = await gateway.chat(active.model_name, messages, tools)
            except Exception as exc:
                raise _RuntimeFailure(
                    "provider_failure", "Модель сейчас не смогла ответить. Попробуйте ещё раз."
                ) from exc

            if not response.tool_calls:
                final_text = response.content.strip()
                if not final_text:
                    raise _RuntimeFailure(
                        "empty_response", "Модель вернула пустой ответ. Попробуйте ещё раз."
                    )
                return await self._finish(user_id, final_text, pending, references, gateway, active)

            if len(response.tool_calls) > self.max_tool_calls_per_step:
                return await self._safe_reply(
                    user_id,
                    "Модель запросила слишком много действий за один шаг.",
                    "tool_call_limit",
                )

            messages.append(
                ChatMessage(
                    role="assistant", content=response.content, tool_calls=response.tool_calls
                )
            )
            for call in response.tool_calls:
                result = await self._execute_tool(call, definitions)
                references.extend(result.references)
                action = self._pending_action(result)
                if action is not None:
                    pending.append(action)
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result.content[: self.max_tool_result_chars],
                        tool_name=call.name,
                        tool_call_id=call.id,
                    )
                )

        return await self._safe_reply(
            user_id,
            "Модель не завершила ответ за допустимое число шагов.",
            "step_limit",
        )

    async def _execute_tool(
        self, call: ToolCall, definitions: dict[str, ToolDefinition]
    ) -> ToolResult:
        definition = definitions.get(call.name)
        if definition is None:
            return self._tool_error("unknown tool")
        try:
            Draft202012Validator(definition.input_schema).validate(call.arguments)
        except JSONSchemaError:
            return self._tool_error("invalid arguments")
        try:
            return await self.tools.call_tool(call.name, call.arguments)
        except Exception:
            return self._tool_error("tool unavailable")

    async def _finish(
        self,
        user_id: int,
        text: str,
        pending: Sequence[PendingActionView],
        references: Sequence[DomainReference],
        gateway,
        active,
    ) -> AgentReply:
        unique_references = self._unique_references(references)
        await self.memory.append_message(
            user_id,
            "assistant",
            text,
            domain_references=[item.model_dump(mode="json") for item in unique_references],
        )

        async def summarize(previous: str, transcript: str) -> str:
            response = await gateway.chat(
                active.model_name,
                [
                    ChatMessage(
                        role="system",
                        content="Кратко суммируй диалог без скрытых рассуждений и секретов.",
                    ),
                    ChatMessage(role="user", content=f"Прошлое резюме: {previous}\n{transcript}"),
                ],
            )
            return response.content.strip()

        await self.memory.compact_conversation(user_id, summarize)
        return AgentReply(
            text=text,
            pending_actions=tuple(pending),
            references=unique_references,
        )

    async def _safe_reply(self, user_id: int, text: str, code: str) -> AgentReply:
        await self.memory.append_message(user_id, "assistant", text)
        return AgentReply(text=text, error_code=code)

    @staticmethod
    def _tool_error(message: str) -> ToolResult:
        return ToolResult(
            content=json.dumps({"error": message}, ensure_ascii=False),
            structured_content={"error": message},
            is_error=True,
        )

    @staticmethod
    def _pending_action(result: ToolResult) -> PendingActionView | None:
        if result.is_error or not result.structured_content:
            return None
        try:
            pending = PendingActionView.model_validate(result.structured_content)
        except ValidationError:
            return None
        return pending if pending.status == PendingActionStatus.PENDING else None

    @staticmethod
    def _unique_references(
        references: Sequence[DomainReference],
    ) -> tuple[DomainReference, ...]:
        unique: list[DomainReference] = []
        seen: set[tuple[str, int]] = set()
        for reference in references:
            key = (reference.kind, reference.id)
            if key not in seen:
                seen.add(key)
                unique.append(reference.model_copy(update={"position": len(unique) + 1}))
        return tuple(unique)
