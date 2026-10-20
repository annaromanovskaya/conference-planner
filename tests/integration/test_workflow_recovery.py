import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import User
from conference_scout.db.repositories.agent_workflows import AgentWorkflowRepository
from conference_scout.schemas.common import AgentRunStatus, AgentTaskType
from conference_scout.schemas.workflows import WorkflowBudget, WorkflowPlan, WorkflowTaskSpec
from conference_scout.workflows.engine import TaskArtifact, WorkflowEngine


@pytest.mark.asyncio
async def test_recovery_keeps_completed_artifact_and_retries_interrupted_task_once(
    session: AsyncSession,
) -> None:
    user = User(telegram_id=30_001)
    session.add(user)
    await session.flush()
    repository = AgentWorkflowRepository(session)
    plan = WorkflowPlan(
        tasks=(
            WorkflowTaskSpec(task_id="search", task_type=AgentTaskType.SEARCH_CANDIDATES),
            WorkflowTaskSpec(
                task_id="crawl",
                task_type=AgentTaskType.CRAWL_CANDIDATE,
                dependencies=("search",),
            ),
        )
    )
    run = await repository.create_run(
        user.id, "Найди конференцию", plan, WorkflowBudget(max_tasks=2)
    )
    search = await repository.claim_ready_task(run.id)
    assert search is not None
    await repository.complete_task(
        search.id, "search_results", {"candidate_ids": [4]}, ("search-hash",)
    )
    interrupted = await repository.claim_ready_task(run.id)
    assert interrupted is not None and interrupted.task_key == "crawl"
    await session.commit()
    calls = {"search": 0, "crawl": 0}

    async def search_handler(_context):
        calls["search"] += 1
        return TaskArtifact(artifact_kind="unexpected", payload={})

    async def crawl_handler(context):
        calls["crawl"] += 1
        assert context.dependencies["search"].payload["candidate_ids"] == [4]
        return TaskArtifact(
            artifact_kind="document",
            payload={"url": "https://event.example/"},
            source_hashes=("page-hash",),
        )

    report = await WorkflowEngine(
        repository,
        {
            AgentTaskType.SEARCH_CANDIDATES: search_handler,
            AgentTaskType.CRAWL_CANDIDATE: crawl_handler,
        },
    ).recover_incomplete_runs()

    assert report.recovered_run_ids == (run.id,)
    assert calls == {"search": 0, "crawl": 1}
    artifacts = await repository.list_artifacts(run.id)
    assert [item.artifact_kind for item in artifacts] == ["search_results", "document"]


@pytest.mark.asyncio
async def test_recovery_skips_partial_run_without_retryable_tasks(
    session: AsyncSession,
) -> None:
    user = User(telegram_id=30_002)
    session.add(user)
    await session.flush()
    repository = AgentWorkflowRepository(session)
    plan = WorkflowPlan(
        tasks=(
            WorkflowTaskSpec(
                task_id="search", task_type=AgentTaskType.SEARCH_CANDIDATES
            ),
        )
    )
    run = await repository.create_run(
        user.id, "Найди конференцию", plan, WorkflowBudget(max_tasks=1)
    )
    task = await repository.claim_ready_task(run.id)
    assert task is not None
    await repository.fail_task(task.id, "INVALID_REQUEST", retryable=False)
    await repository.set_run_state(
        run.id, AgentRunStatus.PARTIAL, error_code="INVALID_REQUEST"
    )
    await session.commit()

    report = await WorkflowEngine(repository, {}).recover_incomplete_runs()

    assert report.recovered_run_ids == ()
    assert report.failed_run_ids == ()
    stored = await repository.get_run_by_id(run.id)
    assert stored.status == AgentRunStatus.PARTIAL
