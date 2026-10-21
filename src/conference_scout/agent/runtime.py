import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaError
from pydantic import BaseModel, ConfigDict, ValidationError

from conference_scout.agent.memory import MemoryService
from conference_scout.agent.prompts import build_system_prompt
from conference_scout.ai.gateway import ModelGatewayError
from conference_scout.ai.models import ChatMessage, ToolCall, ToolDefinition
from conference_scout.ai.registry import ModelRegistry
from conference_scout.mcp.client import ToolClient, ToolResult
from conference_scout.runtime_clock import ApplicationClock
from conference_scout.schemas.agent import DomainReference, PendingActionView
from conference_scout.schemas.common import AgentRunStatus, PendingActionStatus
from conference_scout.workflows.engine import WorkflowEngine
from conference_scout.workflows.planner import (
    WorkflowPlanner,
    is_conference_research_request,
)

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str], Awaitable[None]]


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
        tracked_check_timeout_seconds: float = 300,
        workflow_planner: WorkflowPlanner | None = None,
        workflow_engine: WorkflowEngine | None = None,
        application_clock: ApplicationClock | None = None,
    ) -> None:
        self.registry = registry
        self.memory = memory
        self.tools = tools
        self.max_steps = max_steps
        self.max_tool_calls_per_step = max_tool_calls_per_step
        self.timeout_seconds = timeout_seconds
        self.tracked_check_timeout_seconds = tracked_check_timeout_seconds
        self.max_tool_result_chars = max_tool_result_chars
        self.workflow_planner = workflow_planner
        self.workflow_engine = workflow_engine
        self.application_clock = application_clock

    async def respond(
        self,
        user_id: int,
        text: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> AgentReply:
        try:
            if self._requests_tracked_refresh(text):
                # A tracked refresh is a finite batch whose individual checks are
                # bounded below. Do not cancel the complete batch with the shorter
                # conversational timeout: retain each committed result and continue.
                return await self._respond(user_id, text, progress=progress)
            async with asyncio.timeout(self.timeout_seconds):
                return await self._respond(user_id, text, progress=progress)
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

    async def _respond(
        self,
        user_id: int,
        text: str,
        *,
        progress: ProgressCallback | None,
    ) -> AgentReply:
        if self._requests_tracked_refresh(text):
            return await self._respond_with_tracked_refresh(
                user_id, text, progress=progress
            )
        if (
            self.workflow_planner is not None
            and self.workflow_engine is not None
            and self._requests_research(text)
        ):
            return await self._respond_with_workflow(
                user_id, text, progress=progress
            )
        await self._emit_progress(progress, "Анализирую запрос и выбираю инструменты…")
        context = await self.memory.get_context(user_id, text)
        await self.memory.append_message(user_id, "user", text)
        # MCP mutations use a separate database session. End this SQLite write
        # transaction before invoking tools so the MCP session can acquire its lock.
        await self.memory.session.commit()
        try:
            active = await self.registry.get_active(user_id)
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
                content=build_system_prompt(
                    context,
                    tools_available=bool(tools),
                    clock=self.application_clock,
                ),
            ),
            *(ChatMessage(role=item.role, content=item.content) for item in context.messages),
            ChatMessage(role="user", content=text),
        ]
        definitions = {tool.name: tool for tool in tools}
        references: list[DomainReference] = []
        pending: list[PendingActionView] = []
        receipt_summaries: list[str] = []
        corrective_retry_used = False
        mutation_requested = self._requests_mutation(text)
        research_requested = self._requests_research(text)
        research_tool_succeeded = False

        for step in range(self.max_steps):
            if step:
                await self._emit_progress(progress, "Сопоставляю найденные сведения…")
            try:
                response = await self.registry.chat(user_id, active, messages, tools)
            except ModelGatewayError as exc:
                raise self._provider_runtime_failure(exc) from exc
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
                missing_mutation_tool = (
                    mutation_requested and not pending and not receipt_summaries
                )
                missing_research_tool = research_requested and not research_tool_succeeded
                if (
                    tools
                    and (missing_mutation_tool or missing_research_tool)
                    and not corrective_retry_used
                ):
                    correction = (
                        "Исходный запрос требует изменения данных. Не описывай будущее "
                        "действие и не проси текстовое подтверждение. Сейчас вызови "
                        "подходящий propose_*; если не хватает данных, сначала используй "
                        "доступный инструмент поиска."
                        if missing_mutation_tool
                        else "Исходный запрос требует интернет-поиска. Не утверждай, что "
                        "поиск выполнен по памяти модели. Сейчас вызови "
                        "search_conference_candidates, а подходящие результаты проверяй "
                        "через get_candidate."
                    )
                    messages.extend(
                        (
                            ChatMessage(role="assistant", content=final_text),
                            ChatMessage(role="user", content=correction),
                        )
                    )
                    corrective_retry_used = True
                    continue
                if receipt_summaries:
                    final_text = "\n".join(receipt_summaries)
                elif self._claims_unreceipted_mutation(final_text):
                    final_text = self._remove_unreceipted_mutation_claims(
                        final_text,
                        append_notice=mutation_requested,
                    )
                return await self._finish(user_id, final_text, pending, references, active)

            messages.append(
                ChatMessage(
                    role="assistant", content=response.content, tool_calls=response.tool_calls
                )
            )
            for batch_start in range(0, len(response.tool_calls), self.max_tool_calls_per_step):
                if batch_start:
                    await self._emit_progress(
                        progress,
                        "Часть действий подготовлена. Перехожу к следующей партии…",
                    )
                batch = response.tool_calls[
                    batch_start : batch_start + self.max_tool_calls_per_step
                ]
                for call in batch:
                    await self._emit_progress(progress, self._tool_started_text(call))
                    result = await self._execute_tool(call, definitions)
                    await self._emit_progress(progress, self._tool_finished_text(call, result))
                    if not result.is_error and call.name in {
                        "search_conference_candidates",
                        "get_candidate",
                        "check_tracked_conference",
                    }:
                        research_tool_succeeded = True
                    if result.is_error:
                        logger.warning("agent tool call failed", extra={"tool_name": call.name})
                        if call.name.startswith("propose_"):
                            if call.name == "propose_save_memory":
                                return await self._safe_reply(
                                    user_id,
                                    "Не удалось подготовить запись в долговременную память. "
                                    "Ничего не сохранено; повторите запрос позже.",
                                    "mutation_tool_failure",
                                )
                            return await self._safe_reply(
                                user_id,
                                "Не удалось подготовить действие. Ничего не сохранено, "
                                "и отслеживание не включено. Повторите запрос или укажите "
                                "официальную ссылку конференции.",
                                "mutation_tool_failure",
                            )
                    references.extend(result.references)
                    action = self._pending_action(result)
                    if action is not None:
                        pending.append(action)
                    receipt_summary = self._receipt_summary(result)
                    if receipt_summary is not None:
                        receipt_summaries.append(receipt_summary)
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=result.content[: self.max_tool_result_chars],
                            tool_name=call.name,
                            tool_call_id=call.id,
                        )
                    )
            if pending:
                return await self._finish(
                    user_id,
                    "Подтвердите действие кнопкой ниже.",
                    pending,
                    references,
                    active,
                )
            if receipt_summaries:
                return await self._finish(
                    user_id,
                    "\n".join(receipt_summaries),
                    pending,
                    references,
                    active,
                )

        return await self._safe_reply(
            user_id,
            "Модель не завершила ответ за допустимое число шагов.",
            "step_limit",
        )

    async def _respond_with_tracked_refresh(
        self,
        user_id: int,
        text: str,
        *,
        progress: ProgressCallback | None,
    ) -> AgentReply:
        """Refresh every tracked conference without asking the model to plan it."""
        await self.memory.append_message(user_id, "user", text)
        await self.memory.session.commit()
        await self._emit_progress(
            progress, "Получаю список отслеживаемых конференций…"
        )
        try:
            available = {item.name for item in await self.tools.list_tools()}
        except Exception as exc:
            raise _RuntimeFailure(
                "tool_failure", "Инструменты конференций сейчас недоступны."
            ) from exc
        required = {"list_conferences", "check_tracked_conference"}
        if not required.issubset(available):
            raise _RuntimeFailure(
                "tool_failure", "Инструменты проверки конференций сейчас недоступны."
            )

        listed = await self.tools.call_tool(
            "list_conferences", {"status": "TRACKING", "limit": 50}
        )
        items = listed.structured_content.get("items")
        if listed.is_error or not isinstance(items, list):
            raise _RuntimeFailure(
                "tracked_list_failure",
                "Не удалось получить список отслеживаемых конференций.",
            )
        conferences = [
            item
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and isinstance(item.get("name"), str)
        ]
        if not conferences:
            return await self._save_direct_reply(
                user_id,
                "Отслеживаемых конференций пока нет.",
                references=listed.references,
            )

        rows: list[str] = []
        references = list(listed.references)
        success_count = 0
        for index, conference in enumerate(conferences, start=1):
            conference_id = conference["id"]
            name = conference["name"].strip() or f"Конференция {conference_id}"
            await self._emit_progress(
                progress,
                f"Проверяю {index} из {len(conferences)}: {name}…",
            )
            try:
                async with asyncio.timeout(self.tracked_check_timeout_seconds):
                    result = await self.tools.call_tool(
                        "check_tracked_conference", {"conference_id": conference_id}
                    )
            except TimeoutError:
                await self._emit_progress(
                    progress,
                    f"Проверка «{name}» превысила допустимое время. "
                    "Перехожу к следующей конференции…",
                )
                rows.append(
                    f"{index}. {name}\n"
                    "Превышено время проверки (лимит 5 минут). "
                    "Данные не изменены. Код: CHECK_TIMEOUT."
                )
                continue
            references.extend(result.references)
            structured = result.structured_content
            summary = structured.get("summary") if isinstance(structured, dict) else None
            committed = (
                structured.get("committed") if isinstance(structured, dict) else False
            )
            if not result.is_error and committed is True and isinstance(summary, str):
                success_count += 1
                rows.append(f"{index}. {name}\n{summary.strip()}")
            else:
                failure_summary = (
                    summary.strip()
                    if isinstance(summary, str) and summary.strip()
                    else "Проверить не удалось; данные не изменены."
                )
                rows.append(f"{index}. {name}\n{failure_summary}")

        failed_count = len(conferences) - success_count
        reply_text = (
            "Проверка отслеживаемых конференций завершена.\n\n"
            f"Проверено: {len(conferences)}.\n"
            f"Успешно: {success_count}.\n"
            f"С ошибкой: {failed_count}.\n\n"
            + "\n\n".join(rows)
        )
        return await self._save_direct_reply(
            user_id,
            reply_text,
            references=self._unique_references(references),
        )

    async def _save_direct_reply(
        self,
        user_id: int,
        text: str,
        *,
        references: Sequence[DomainReference] = (),
    ) -> AgentReply:
        unique_references = self._unique_references(references)
        await self.memory.append_message(
            user_id,
            "assistant",
            text,
            domain_references=[item.model_dump(mode="json") for item in unique_references],
        )
        await self.memory.session.commit()
        return AgentReply(text=text, references=unique_references)

    async def _respond_with_workflow(
        self,
        user_id: int,
        text: str,
        *,
        progress: ProgressCallback | None,
    ) -> AgentReply:
        await self.memory.append_message(user_id, "user", text)
        plan = await self.workflow_planner.plan(user_id, text)

        class CallbackProgress:
            async def emit(self, _run_id, event) -> None:
                if progress is not None:
                    await progress(event.message)

        run = await self.workflow_engine.start(
            user_id,
            text,
            plan,
            CallbackProgress(),
        )
        if run.status == AgentRunStatus.COMPLETED:
            reply_text = f"Исследование завершено. Номер запуска: {run.id}."
            error_code = None
        elif run.status == AgentRunStatus.PARTIAL:
            error_code = self._safe_workflow_error_code(run.error_code or "workflow_partial")
            reply_text = (
                f"Исследование завершено частично. Номер запуска: {run.id}. "
                "Готовые результаты сохранены; продолжение можно возобновить. "
                f"Код: {error_code}."
            )
        else:
            error_code = self._safe_workflow_error_code(run.error_code)
            reply_text = (
                f"Исследование не завершено. Номер запуска: {run.id}. "
                f"Код: {error_code}."
            )
        await self.memory.append_message(user_id, "assistant", reply_text)
        await self.memory.session.commit()
        return AgentReply(text=reply_text, error_code=error_code)

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
            try:
                response = await self.registry.chat(
                    user_id,
                    active,
                    [
                        ChatMessage(
                            role="system",
                            content="Кратко суммируй диалог без скрытых рассуждений и секретов.",
                        ),
                        ChatMessage(
                            role="user",
                            content=f"Прошлое резюме: {previous}\n{transcript}",
                        ),
                    ],
                )
            except ModelGatewayError as exc:
                raise self._provider_runtime_failure(exc) from exc
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
    def _provider_runtime_failure(error: ModelGatewayError) -> _RuntimeFailure:
        if error.code == "free_exhausted":
            return _RuntimeFailure(
                "free_exhausted",
                "Ollama Free usage исчерпан. Выберите другую бесплатную модель через /model.",
            )
        if error.code == "rate_limited":
            return _RuntimeFailure(
                "rate_limited",
                "Ollama временно ограничила запросы. Попробуйте позже или выберите "
                "другую бесплатную модель через /model.",
            )
        return _RuntimeFailure(
            "provider_failure", "Модель сейчас не смогла ответить. Попробуйте ещё раз."
        )

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
    def _receipt_summary(result: ToolResult) -> str | None:
        value = result.structured_content
        if (
            result.is_error
            or not isinstance(value, dict)
            or value.get("kind") != "persistence_receipt"
            or value.get("committed") is not True
        ):
            return None
        summary = value.get("summary")
        return summary[:2_000] if isinstance(summary, str) and summary.strip() else None

    @staticmethod
    def _claims_unreceipted_mutation(text: str) -> bool:
        return bool(
            re.search(
                r"\b(сохранил[аи]?|сохранен[аоы]?|добавил[аи]?|добавлен[аоы]?|"
                r"обновил[аи]?|обновлен[аоы]?)\b|"
                r"\b(?:теперь|уже)\s+(?:\S+\s+){0,3}отслежива(?:ю|ется|ем)\b",
                text.casefold(),
            )
        )

    @staticmethod
    def _requests_mutation(text: str) -> bool:
        return bool(
            re.search(
                r"\b(добав(?:ь|ляй|ьте)|сохран(?:и|яй|ите)|внес(?:и|ите)|"
                r"запиш(?:и|ите)|отслеживай(?:те)?|архивируй(?:те)?|удал(?:и|ите))\b",
                text.casefold(),
            )
        )

    @staticmethod
    def _requests_research(text: str) -> bool:
        return is_conference_research_request(text)

    @staticmethod
    def _requests_tracked_refresh(text: str) -> bool:
        value = text.casefold()
        asks_to_refresh = bool(
            re.search(
                r"\b(проверь(?:те)?|перепроверь(?:те)?|обнов(?:и|ите|лять))\w*\b",
                value,
            )
        )
        tracked = bool(re.search(r"\bотслежива\w*\b", value))
        conference_context = bool(
            re.search(r"\b(конференц|мероприят|дедлайн)\w*\b", value)
        )
        return asks_to_refresh and tracked and conference_context

    @staticmethod
    def _safe_workflow_error_code(value: str | None) -> str:
        code = re.sub(r"[^A-Z0-9_-]", "", (value or "workflow_failed").upper())
        return code[:100] or "WORKFLOW_FAILED"

    @classmethod
    def _remove_unreceipted_mutation_claims(
        cls,
        text: str,
        *,
        append_notice: bool,
    ) -> str:
        sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+|\n+", text)]
        kept = [item for item in sentences if item and not cls._claims_unreceipted_mutation(item)]
        if not kept:
            return "Данные не изменялись: подтверждённой квитанции сохранения или обновления нет."
        result = " ".join(kept)
        if append_notice:
            result += "\n\nВ базу данных ничего не записано."
        return result

    @staticmethod
    async def _emit_progress(callback: ProgressCallback | None, text: str | None) -> None:
        if callback is None or not text:
            return
        try:
            await callback(text[:500])
        except Exception:
            logger.debug("agent progress callback failed", exc_info=True)

    @staticmethod
    def _tool_started_text(call: ToolCall) -> str:
        if call.name == "search_conference_candidates":
            page = call.arguments.get("page", 1)
            return f"Ищу конференции в интернете: страница {page}…"
        if call.name == "get_candidate":
            candidate_id = call.arguments.get("candidate_id", "?")
            return f"Открываю и проверяю источник кандидата {candidate_id}…"
        if call.name == "check_tracked_conference":
            return "Проверяю сохранённую конференцию и её дедлайны…"
        if call.name.startswith("propose_"):
            return "Готовлю действие для вашего подтверждения…"
        return "Получаю необходимые данные…"

    @staticmethod
    def _tool_finished_text(call: ToolCall, result: ToolResult) -> str:
        if result.is_error:
            return "Инструмент не ответил. Продолжаю с доступными данными…"
        structured = result.structured_content
        if call.name == "search_conference_candidates":
            items = structured.get("items") if isinstance(structured, dict) else None
            count = len(items) if isinstance(items, list) else 0
            new_count = structured.get("new_count") if isinstance(structured, dict) else None
            new_text = f", новых: {new_count}" if isinstance(new_count, int) else ""
            return f"Получено кандидатов: {count}{new_text}. Отбираю подходящие источники…"
        if call.name == "get_candidate" and isinstance(structured, dict):
            url = structured.get("url")
            research = structured.get("research")
            facts = research.get("facts") if isinstance(research, dict) else None
            deadlines = research.get("deadlines") if isinstance(research, dict) else None
            fact_count = len(facts) if isinstance(facts, list) else 0
            deadline_count = len(deadlines) if isinstance(deadlines, list) else 0
            if isinstance(url, str):
                return (
                    f"Источник проверен: {url[:250]}. "
                    f"Фактов: {fact_count}, дедлайнов: {deadline_count}."
                )
        if call.name == "check_tracked_conference":
            return "Проверка конференции завершена. Сверяю изменения…"
        return "Данные получены. Формирую следующий шаг…"

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
