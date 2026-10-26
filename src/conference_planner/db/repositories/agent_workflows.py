from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.db.models import AgentArtifact, AgentProgressEvent, AgentRun, AgentTask
from conference_planner.schemas.common import (
    AgentRunStatus,
    AgentTaskStatus,
    ProgressEventType,
    WorkflowKind,
)
from conference_planner.schemas.workflows import WorkflowBudget, WorkflowPlan


class AgentWorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(
        self,
        user_id: int,
        request_text: str,
        plan: WorkflowPlan,
        budget: WorkflowBudget,
        *,
        workflow_kind: WorkflowKind = WorkflowKind.CONFERENCE_RESEARCH,
    ) -> AgentRun:
        run = AgentRun(
            user_id=user_id,
            request_text=request_text,
            workflow_kind=workflow_kind,
            status=AgentRunStatus.PLANNED,
            plan=plan.model_dump(mode="json"),
            budget=budget.model_dump(mode="json"),
            usage={},
        )
        self.session.add(run)
        await self.session.flush()
        self.session.add_all(
            AgentTask(
                run_id=run.id,
                task_key=task.task_id,
                task_type=task.task_type,
                status=AgentTaskStatus.PENDING,
                dependencies=list(task.dependencies),
                input_data=task.input_data,
            )
            for task in plan.tasks
        )
        await self.session.flush()
        return run

    async def find_active_run(
        self, user_id: int, workflow_kind: WorkflowKind
    ) -> AgentRun | None:
        return await self.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.user_id == user_id,
                AgentRun.workflow_kind == workflow_kind,
                AgentRun.status.in_(
                    {
                        AgentRunStatus.PLANNED,
                        AgentRunStatus.RUNNING,
                        AgentRunStatus.PARTIAL,
                    }
                ),
            )
            .order_by(AgentRun.id.desc())
            .limit(1)
        )

    async def latest_run(
        self, user_id: int, workflow_kind: WorkflowKind
    ) -> AgentRun | None:
        return await self.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.user_id == user_id,
                AgentRun.workflow_kind == workflow_kind,
            )
            .order_by(AgentRun.id.desc())
            .limit(1)
        )

    async def get_run(self, user_id: int, run_id: int) -> AgentRun:
        run = await self.session.scalar(
            select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user_id)
        )
        if run is None:
            raise ValueError("run not found")
        return run

    async def list_tasks(self, run_id: int) -> list[AgentTask]:
        return list(
            await self.session.scalars(
                select(AgentTask).where(AgentTask.run_id == run_id).order_by(AgentTask.id)
            )
        )

    async def list_artifacts(self, run_id: int) -> list[AgentArtifact]:
        return list(
            await self.session.scalars(
                select(AgentArtifact)
                .where(AgentArtifact.run_id == run_id)
                .order_by(AgentArtifact.id)
            )
        )

    async def get_run_by_id(self, run_id: int) -> AgentRun:
        run = await self.session.get(AgentRun, run_id)
        if run is None:
            raise ValueError("run not found")
        return run

    async def update_usage(self, run_id: int, usage: dict) -> AgentRun:
        run = await self.get_run_by_id(run_id)
        run.usage = dict(usage)
        await self.session.flush()
        return run

    async def set_run_state(
        self,
        run_id: int,
        status: AgentRunStatus,
        *,
        error_code: str | None = None,
    ) -> AgentRun:
        run = await self.get_run_by_id(run_id)
        run.status = status
        run.error_code = error_code
        await self.session.flush()
        return run

    async def cancel_run(self, user_id: int, run_id: int) -> AgentRun:
        run = await self.get_run(user_id, run_id)
        tasks = await self.list_tasks(run.id)
        for task in tasks:
            if task.status in {AgentTaskStatus.PENDING, AgentTaskStatus.RUNNING}:
                task.status = AgentTaskStatus.CANCELLED
        run.status = AgentRunStatus.CANCELLED
        run.error_code = None
        await self.session.flush()
        return run

    async def claim_ready_task(self, run_id: int) -> AgentTask | None:
        tasks = await self.list_tasks(run_id)
        completed = {
            task.task_key for task in tasks if task.status == AgentTaskStatus.COMPLETED
        }
        for task in tasks:
            if task.status != AgentTaskStatus.PENDING:
                continue
            if not set(task.dependencies).issubset(completed):
                continue
            result = await self.session.execute(
                update(AgentTask)
                .where(
                    AgentTask.id == task.id,
                    AgentTask.status == AgentTaskStatus.PENDING,
                )
                .values(
                    status=AgentTaskStatus.RUNNING,
                    attempts=AgentTask.attempts + 1,
                )
            )
            if result.rowcount != 1:
                continue
            run = await self.session.get(AgentRun, run_id)
            if run is not None:
                run.status = AgentRunStatus.RUNNING
            await self.session.flush()
            return await self.session.get(AgentTask, task.id, populate_existing=True)
        return None

    async def complete_task(
        self,
        task_id: int,
        artifact_kind: str,
        payload: dict,
        source_hashes: tuple[str, ...],
    ) -> AgentArtifact:
        task = await self._require_task(task_id)
        task.status = AgentTaskStatus.COMPLETED
        task.retryable = False
        task.error_code = None
        artifact = AgentArtifact(
            run_id=task.run_id,
            task_id=task.id,
            artifact_kind=artifact_kind,
            payload=payload,
            source_hashes=list(source_hashes),
        )
        self.session.add(artifact)
        await self.session.flush()
        remaining = await self.session.scalar(
            select(AgentTask.id).where(
                AgentTask.run_id == task.run_id,
                AgentTask.status != AgentTaskStatus.COMPLETED,
            ).limit(1)
        )
        if remaining is None:
            run = await self.session.get(AgentRun, task.run_id)
            if run is not None:
                run.status = AgentRunStatus.COMPLETED
            await self.session.flush()
        return artifact

    async def fail_task(self, task_id: int, error_code: str, retryable: bool) -> AgentTask:
        task = await self._require_task(task_id)
        task.status = AgentTaskStatus.FAILED
        task.error_code = error_code
        task.retryable = retryable
        run = await self.session.get(AgentRun, task.run_id)
        if run is not None:
            run.status = AgentRunStatus.PARTIAL if retryable else AgentRunStatus.FAILED
            run.error_code = error_code
        await self.session.flush()
        return task

    async def resume_run(self, run_id: int) -> AgentRun:
        run = await self.session.get(AgentRun, run_id)
        if run is None:
            raise ValueError("run not found")
        tasks = await self.list_tasks(run_id)
        for task in tasks:
            if task.status == AgentTaskStatus.RUNNING or (
                task.status == AgentTaskStatus.FAILED and task.retryable
            ):
                task.status = AgentTaskStatus.PENDING
                task.retryable = False
                task.error_code = None
        run.status = AgentRunStatus.PLANNED
        run.error_code = None
        await self.session.flush()
        return run

    async def list_recoverable_runs(
        self, workflow_kind: WorkflowKind | None = None
    ) -> list[AgentRun]:
        conditions = [
            AgentRun.status.in_(
                {
                    AgentRunStatus.PLANNED,
                    AgentRunStatus.RUNNING,
                    AgentRunStatus.PARTIAL,
                }
            )
        ]
        if workflow_kind is not None:
            conditions.append(AgentRun.workflow_kind == workflow_kind)
        return list(
            await self.session.scalars(
                select(AgentRun)
                .where(*conditions)
                .order_by(AgentRun.id)
            )
        )

    async def append_progress(
        self,
        run_id: int,
        event_type: ProgressEventType,
        message: str,
        safe_data: dict[str, str],
    ) -> AgentProgressEvent:
        if await self.session.get(AgentRun, run_id) is None:
            raise ValueError("run not found")
        latest = await self.session.scalar(
            select(func.max(AgentProgressEvent.sequence)).where(
                AgentProgressEvent.run_id == run_id
            )
        )
        event = AgentProgressEvent(
            run_id=run_id,
            sequence=(latest or 0) + 1,
            event_type=event_type,
            message=message,
            safe_data=safe_data,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_progress_after(
        self, run_id: int, sequence: int
    ) -> list[AgentProgressEvent]:
        return list(
            await self.session.scalars(
                select(AgentProgressEvent)
                .where(
                    AgentProgressEvent.run_id == run_id,
                    AgentProgressEvent.sequence > sequence,
                )
                .order_by(AgentProgressEvent.sequence)
            )
        )

    async def _require_task(self, task_id: int) -> AgentTask:
        task = await self.session.get(AgentTask, task_id)
        if task is None:
            raise ValueError("task not found")
        return task
