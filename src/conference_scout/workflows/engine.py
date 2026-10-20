import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from conference_scout.db.models import AgentArtifact, AgentRun, AgentTask
from conference_scout.db.repositories.agent_workflows import AgentWorkflowRepository
from conference_scout.schemas.common import (
    AgentRunStatus,
    AgentTaskType,
    ArtifactValidationState,
    ProgressEventType,
    WorkflowKind,
)
from conference_scout.schemas.workflows import AgentRunView, WorkflowBudget, WorkflowPlan
from conference_scout.workflows.progress import ProgressEvent


class WorkflowBudgetExceeded(ValueError):
    pass


class WorkflowTaskFailure(RuntimeError):
    def __init__(self, error_code: str, *, retryable: bool) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable


class TaskArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_kind: str = Field(min_length=1, max_length=100)
    payload: dict = Field(default_factory=dict)
    source_hashes: tuple[str, ...] = ()
    validation_state: ArtifactValidationState = ArtifactValidationState.UNVALIDATED
    model_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    pages: int = Field(default=0, ge=0)
    characters: int = Field(default=0, ge=0)


@dataclass(frozen=True, slots=True)
class WorkflowTaskContext:
    run: AgentRun
    task: AgentTask
    dependencies: Mapping[str, AgentArtifact]
    budget: WorkflowBudget
    usage: Mapping[str, int]


class ProgressSink(Protocol):
    async def emit(self, run_id: int, event: ProgressEvent) -> None: ...


TaskHandler = Callable[[WorkflowTaskContext], Awaitable[TaskArtifact]]


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    recovered_run_ids: tuple[int, ...]
    failed_run_ids: tuple[int, ...]


class WorkflowEngine:
    def __init__(
        self,
        repository: AgentWorkflowRepository,
        handlers: Mapping[AgentTaskType, TaskHandler],
    ) -> None:
        self.repository = repository
        self.handlers = dict(handlers)

    async def start(
        self,
        user_id: int,
        request_text: str,
        plan: WorkflowPlan,
        progress: ProgressSink | None = None,
        *,
        budget: WorkflowBudget | None = None,
        workflow_kind: WorkflowKind = WorkflowKind.CONFERENCE_RESEARCH,
    ) -> AgentRunView:
        effective_budget = budget or WorkflowBudget()
        self._validate_budget(plan, effective_budget)
        run = await self.repository.create_run(
            user_id,
            request_text,
            plan,
            effective_budget,
            workflow_kind=workflow_kind,
        )
        await self.repository.session.commit()
        await self._emit(
            progress,
            run.id,
            ProgressEvent(
                event_type=ProgressEventType.AGENT_STARTED,
                message="Запущен план исследования конференций.",
            ),
        )
        return await self._execute(run.id, progress)

    async def resume(
        self,
        run_id: int,
        progress: ProgressSink | None = None,
    ) -> AgentRunView:
        run = await self.repository.get_run_by_id(run_id)
        if run.status == AgentRunStatus.COMPLETED:
            return AgentRunView.model_validate(run)
        await self.repository.resume_run(run_id)
        await self.repository.session.commit()
        return await self._execute(run_id, progress)

    async def cancel(self, user_id: int, run_id: int) -> AgentRunView:
        run = await self.repository.cancel_run(user_id, run_id)
        await self.repository.session.commit()
        return AgentRunView.model_validate(run)

    async def recover_incomplete_runs(
        self,
        progress: ProgressSink | None = None,
        *,
        workflow_kind: WorkflowKind | None = None,
    ) -> RecoveryReport:
        recovered: list[int] = []
        failed: list[int] = []
        for run in await self.repository.list_recoverable_runs(workflow_kind):
            tasks = await self.repository.list_tasks(run.id)
            if run.status == AgentRunStatus.PARTIAL and not any(
                task.retryable for task in tasks
            ):
                continue
            try:
                result = await self.resume(run.id, progress)
            except Exception:
                await self.repository.set_run_state(
                    run.id, AgentRunStatus.PARTIAL, error_code="RECOVERY_FAILED"
                )
                await self.repository.session.commit()
                failed.append(run.id)
                continue
            if result.status == AgentRunStatus.COMPLETED:
                recovered.append(run.id)
            else:
                failed.append(run.id)
        return RecoveryReport(tuple(recovered), tuple(failed))

    async def _execute(
        self,
        run_id: int,
        progress: ProgressSink | None,
    ) -> AgentRunView:
        run = await self.repository.get_run_by_id(run_id)
        budget = WorkflowBudget.model_validate(run.budget)
        try:
            async with asyncio.timeout(budget.timeout_seconds):
                while task := await self.repository.claim_ready_task(run_id):
                    result = await self._execute_task(run, task, budget, progress)
                    if result is not None:
                        return result
        except TimeoutError:
            await self.repository.set_run_state(
                run_id, AgentRunStatus.PARTIAL, error_code="WORKFLOW_TIMEOUT"
            )
            await self.repository.session.commit()
        run = await self.repository.get_run_by_id(run_id)
        return AgentRunView.model_validate(run)

    async def _execute_task(
        self,
        run: AgentRun,
        task: AgentTask,
        budget: WorkflowBudget,
        progress: ProgressSink | None,
    ) -> AgentRunView | None:
        handler = self.handlers.get(task.task_type)
        if handler is None:
            await self.repository.fail_task(task.id, "HANDLER_NOT_CONFIGURED", retryable=False)
            await self.repository.session.commit()
            return AgentRunView.model_validate(await self.repository.get_run_by_id(run.id))
        dependencies = await self._dependency_artifacts(run.id, task)
        usage = self._usage(run)
        await self._emit(
            progress,
            run.id,
            ProgressEvent(
                event_type=ProgressEventType.TOOL_STARTED,
                message=self._task_started_message(task.task_type),
                safe_data={"task": task.task_key},
            ),
        )
        try:
            artifact = await handler(
                WorkflowTaskContext(run, task, dependencies, budget, usage)
            )
        except WorkflowTaskFailure as exc:
            await self.repository.fail_task(
                task.id, exc.error_code, retryable=exc.retryable
            )
            await self.repository.session.commit()
            return AgentRunView.model_validate(await self.repository.get_run_by_id(run.id))
        projected = self._project_usage(usage, artifact)
        error_code = self._budget_error(projected, budget)
        if error_code is not None:
            await self.repository.fail_task(task.id, error_code, retryable=False)
            await self.repository.set_run_state(
                run.id, AgentRunStatus.PARTIAL, error_code=error_code
            )
            await self.repository.session.commit()
            return AgentRunView.model_validate(await self.repository.get_run_by_id(run.id))
        stored = await self.repository.complete_task(
            task.id,
            artifact.artifact_kind,
            artifact.payload,
            artifact.source_hashes,
        )
        stored.validation_state = artifact.validation_state
        projected["tasks_completed"] = projected.get("tasks_completed", 0) + 1
        await self.repository.update_usage(run.id, projected)
        await self.repository.session.commit()
        await self._emit(
            progress,
            run.id,
            ProgressEvent(
                event_type=ProgressEventType.TOOL_COMPLETED,
                message=self._task_completed_message(
                    task.task_type, artifact.artifact_kind
                ),
                safe_data={"artifact": artifact.artifact_kind},
            ),
        )
        return None

    async def _dependency_artifacts(
        self, run_id: int, task: AgentTask
    ) -> dict[str, AgentArtifact]:
        tasks = {item.id: item for item in await self.repository.list_tasks(run_id)}
        by_key = {item.task_key: item for item in tasks.values()}
        artifacts = await self.repository.list_artifacts(run_id)
        by_task = {item.task_id: item for item in artifacts}
        return {
            key: by_task[by_key[key].id]
            for key in task.dependencies
            if key in by_key and by_key[key].id in by_task
        }

    @staticmethod
    def _validate_budget(plan: WorkflowPlan, budget: WorkflowBudget) -> None:
        if len(plan.tasks) > budget.max_tasks:
            raise WorkflowBudgetExceeded("max_tasks exceeded")

    @staticmethod
    def _usage(run: AgentRun) -> dict[str, int]:
        return {
            key: int(value)
            for key, value in run.usage.items()
            if isinstance(value, int)
        }

    @staticmethod
    def _project_usage(usage: Mapping[str, int], artifact: TaskArtifact) -> dict[str, int]:
        projected = dict(usage)
        increments = {
            "model_calls": artifact.model_calls,
            "tokens": artifact.tokens,
            "pages": artifact.pages,
            "characters": artifact.characters,
        }
        for key, value in increments.items():
            projected[key] = projected.get(key, 0) + value
        return projected

    @staticmethod
    def _budget_error(usage: Mapping[str, int], budget: WorkflowBudget) -> str | None:
        if usage.get("model_calls", 0) > budget.max_model_calls:
            return "MODEL_CALL_BUDGET_EXCEEDED"
        if usage.get("tokens", 0) > budget.max_tokens:
            return "TOKEN_BUDGET_EXCEEDED"
        if usage.get("pages", 0) > budget.max_pages:
            return "PAGE_BUDGET_EXCEEDED"
        if usage.get("characters", 0) > budget.max_characters:
            return "CHARACTER_BUDGET_EXCEEDED"
        return None

    @staticmethod
    def _task_started_message(task_type: AgentTaskType) -> str:
        messages = {
            AgentTaskType.CHECK_TRACKED_CONFERENCES: (
                "МОНИТОРИНГ проверяет отслеживаемые конференции и дедлайны."
            ),
            AgentTaskType.RECHECK_SKIPPED_CANDIDATES: (
                "КООРДИНАТОР сверяет решения и изменившиеся пропущенные страницы."
            ),
            AgentTaskType.SEARCH_CANDIDATES: (
                "КООРДИНАТОР вызвал поиск кандидатов, чтобы найти источники."
            ),
            AgentTaskType.CRAWL_CANDIDATE: (
                "ИССЛЕДОВАТЕЛЬ открыл страницу, чтобы собрать доказательства."
            ),
            AgentTaskType.APPLY_PARSER_RECIPE: (
                "ПАРСЕР применяет сохранённый рецепт к странице."
            ),
            AgentTaskType.EXTRACT_RESEARCH: (
                "ИССЛЕДОВАТЕЛЬ извлекает даты, дедлайны и ссылки из источника."
            ),
            AgentTaskType.PROPOSE_PARSER_RECIPE: (
                "ИССЛЕДОВАТЕЛЬ предлагает безопасный рецепт парсинга."
            ),
            AgentTaskType.EVALUATE_RELEVANCE: (
                "КООРДИНАТОР оценивает соответствие конференции темам."
            ),
            AgentTaskType.VERIFY_RESEARCH: (
                "ПРОВЕРЯЛЬЩИК сверяет факты с цитатами и URL."
            ),
            AgentTaskType.PROPOSE_PERSISTENCE: (
                "КООРДИНАТОР готовит проверенного кандидата без записи в базу."
            ),
            AgentTaskType.READ_DATABASE: "КООРДИНАТОР читает сохранённые данные.",
            AgentTaskType.SYNTHESIZE_RESPONSE: (
                "КООРДИНАТОР формирует итоговый ответ."
            ),
        }
        return messages[task_type]

    @staticmethod
    def _task_completed_message(
        task_type: AgentTaskType, artifact_kind: str
    ) -> str:
        role = {
            AgentTaskType.CHECK_TRACKED_CONFERENCES: "МОНИТОРИНГ",
            AgentTaskType.RECHECK_SKIPPED_CANDIDATES: "КООРДИНАТОР",
            AgentTaskType.SEARCH_CANDIDATES: "ИССЛЕДОВАТЕЛЬ",
            AgentTaskType.CRAWL_CANDIDATE: "ИССЛЕДОВАТЕЛЬ",
            AgentTaskType.APPLY_PARSER_RECIPE: "ПАРСЕР",
            AgentTaskType.EXTRACT_RESEARCH: "ИССЛЕДОВАТЕЛЬ",
            AgentTaskType.PROPOSE_PARSER_RECIPE: "ИССЛЕДОВАТЕЛЬ",
            AgentTaskType.EVALUATE_RELEVANCE: "КООРДИНАТОР",
            AgentTaskType.VERIFY_RESEARCH: "ПРОВЕРЯЛЬЩИК",
            AgentTaskType.PROPOSE_PERSISTENCE: "КООРДИНАТОР",
            AgentTaskType.READ_DATABASE: "КООРДИНАТОР",
            AgentTaskType.SYNTHESIZE_RESPONSE: "КООРДИНАТОР",
        }[task_type]
        return f"{role} завершил инструмент. Результат: {artifact_kind}."

    async def _emit(
        self,
        progress: ProgressSink | None,
        run_id: int,
        event: ProgressEvent,
    ) -> None:
        if progress is not None:
            await progress.emit(run_id, event)
            await self.repository.session.commit()
