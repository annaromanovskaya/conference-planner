import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import User
from conference_scout.db.repositories.agent_workflows import AgentWorkflowRepository
from conference_scout.schemas.common import AgentRunStatus, AgentTaskType, WorkflowKind
from conference_scout.schemas.workflows import WorkflowBudget, WorkflowPlan, WorkflowTaskSpec
from conference_scout.workflows.engine import (
    TaskArtifact,
    WorkflowBudgetExceeded,
    WorkflowEngine,
    WorkflowTaskFailure,
)


class ProgressCollector:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def emit(self, _run_id, event) -> None:
        self.messages.append(event.message)


def plan() -> WorkflowPlan:
    return WorkflowPlan(
        tasks=(
            WorkflowTaskSpec(task_id="search", task_type=AgentTaskType.SEARCH_CANDIDATES),
            WorkflowTaskSpec(
                task_id="crawl",
                task_type=AgentTaskType.CRAWL_CANDIDATE,
                dependencies=("search",),
            ),
        )
    )


@pytest.mark.asyncio
async def test_engine_rejects_plan_before_execution_when_budget_is_too_small(
    session: AsyncSession,
) -> None:
    user = User(telegram_id=20_001)
    session.add(user)
    await session.flush()
    called = False

    async def handler(_context):
        nonlocal called
        called = True
        return TaskArtifact(artifact_kind="unused", payload={})

    engine = WorkflowEngine(
        AgentWorkflowRepository(session),
        {AgentTaskType.SEARCH_CANDIDATES: handler},
    )

    with pytest.raises(WorkflowBudgetExceeded, match="max_tasks"):
        await engine.start(
            user.id,
            "Найди конференции",
            plan(),
            budget=WorkflowBudget(max_tasks=1),
        )

    assert called is False


@pytest.mark.asyncio
async def test_engine_resume_retries_only_failed_task_and_reuses_completed_artifact(
    session: AsyncSession,
) -> None:
    user = User(telegram_id=20_002)
    session.add(user)
    await session.flush()
    calls = {"search": 0, "crawl": 0}

    async def search(context):
        calls["search"] += 1
        return TaskArtifact(
            artifact_kind="search_results",
            payload={"candidate_ids": [7]},
            source_hashes=("search-hash",),
        )

    async def crawl(context):
        calls["crawl"] += 1
        assert context.dependencies["search"].payload == {"candidate_ids": [7]}
        if calls["crawl"] == 1:
            raise WorkflowTaskFailure("FETCH_TIMEOUT", retryable=True)
        return TaskArtifact(
            artifact_kind="document",
            payload={"url": "https://event.example/"},
            source_hashes=("page-hash",),
            pages=1,
            characters=100,
        )

    repository = AgentWorkflowRepository(session)
    engine = WorkflowEngine(
        repository,
        {
            AgentTaskType.SEARCH_CANDIDATES: search,
            AgentTaskType.CRAWL_CANDIDATE: crawl,
        },
    )

    partial = await engine.start(
        user.id,
        "Найди конференцию",
        plan(),
        budget=WorkflowBudget(max_tasks=2),
    )
    completed = await engine.resume(partial.id)

    assert partial.status == AgentRunStatus.PARTIAL
    assert completed.status == AgentRunStatus.COMPLETED
    assert calls == {"search": 1, "crawl": 2}
    artifacts = await repository.list_artifacts(completed.id)
    assert [item.artifact_kind for item in artifacts] == ["search_results", "document"]


@pytest.mark.asyncio
async def test_engine_stops_before_handler_that_would_exceed_model_call_budget(
    session: AsyncSession,
) -> None:
    user = User(telegram_id=20_003)
    session.add(user)
    await session.flush()

    async def search(_context):
        return TaskArtifact(
            artifact_kind="search_results",
            payload={},
            model_calls=1,
            tokens=100,
        )

    async def crawl(_context):
        return TaskArtifact(
            artifact_kind="document",
            payload={},
            model_calls=1,
            tokens=100,
        )

    engine = WorkflowEngine(
        AgentWorkflowRepository(session),
        {
            AgentTaskType.SEARCH_CANDIDATES: search,
            AgentTaskType.CRAWL_CANDIDATE: crawl,
        },
    )

    result = await engine.start(
        user.id,
        "Исследуй",
        plan(),
        budget=WorkflowBudget(max_tasks=2, max_model_calls=1),
    )

    assert result.status == AgentRunStatus.PARTIAL
    assert result.error_code == "MODEL_CALL_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_engine_cancel_checks_owner_and_cancels_pending_tasks(
    session: AsyncSession,
) -> None:
    first = User(telegram_id=20_004)
    second = User(telegram_id=20_005)
    session.add_all([first, second])
    await session.flush()
    repository = AgentWorkflowRepository(session)
    run = await repository.create_run(
        first.id,
        "Исследуй",
        plan(),
        WorkflowBudget(max_tasks=2),
    )
    engine = WorkflowEngine(repository, {})

    with pytest.raises(ValueError, match="run not found"):
        await engine.cancel(second.id, run.id)
    cancelled = await engine.cancel(first.id, run.id)

    assert cancelled.status == AgentRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_engine_progress_names_role_tool_purpose_and_result(
    session: AsyncSession,
) -> None:
    user = User(telegram_id=20_006)
    session.add(user)
    await session.flush()

    async def search(_context):
        return TaskArtifact(
            artifact_kind="candidate_list", payload={"candidate_ids": [7]}
        )

    progress = ProgressCollector()
    await WorkflowEngine(
        AgentWorkflowRepository(session),
        {AgentTaskType.SEARCH_CANDIDATES: search},
    ).start(
        user.id,
        "Найди конференцию",
        WorkflowPlan(
            tasks=(
                WorkflowTaskSpec(
                    task_id="search", task_type=AgentTaskType.SEARCH_CANDIDATES
                ),
            )
        ),
        progress,
    )

    assert any(
        "КООРДИНАТОР" in message and "поиск кандидатов" in message
        for message in progress.messages
    )
    assert any(
        "ИССЛЕДОВАТЕЛЬ" in message and "candidate_list" in message
        for message in progress.messages
    )


@pytest.mark.asyncio
async def test_engine_persists_workflow_kind_and_finds_owner_active_run(
    session: AsyncSession,
) -> None:
    first = User(telegram_id=20_007)
    second = User(telegram_id=20_008)
    session.add_all((first, second))
    await session.flush()
    repository = AgentWorkflowRepository(session)
    run = await repository.create_run(
        first.id,
        "Цикл конференций",
        plan(),
        WorkflowBudget(max_tasks=2),
        workflow_kind=WorkflowKind.CONFERENCE_CYCLE,
    )

    assert run.workflow_kind == WorkflowKind.CONFERENCE_CYCLE
    assert (
        await repository.find_active_run(first.id, WorkflowKind.CONFERENCE_CYCLE)
    ).id == run.id
    assert await repository.find_active_run(second.id, WorkflowKind.CONFERENCE_CYCLE) is None
    assert await repository.find_active_run(first.id, WorkflowKind.CONFERENCE_RESEARCH) is None
    recoverable = await repository.list_recoverable_runs(
        WorkflowKind.CONFERENCE_CYCLE
    )
    assert [item.id for item in recoverable] == [run.id]
    assert await repository.list_recoverable_runs(WorkflowKind.CONFERENCE_RESEARCH) == []
