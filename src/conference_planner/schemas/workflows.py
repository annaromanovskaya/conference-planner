from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conference_planner.schemas.common import (
    AgentRunStatus,
    AgentTaskStatus,
    AgentTaskType,
    ArtifactValidationState,
    WorkflowKind,
)


class WorkflowBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tasks: int = Field(default=20, ge=1, le=200)
    max_model_calls: int = Field(default=8, ge=0, le=100)
    max_tokens: int = Field(default=100_000, ge=0)
    max_pages: int = Field(default=20, ge=0, le=500)
    max_characters: int = Field(default=500_000, ge=0)
    timeout_seconds: int = Field(default=300, ge=1, le=3_600)


class WorkflowTaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_-]*$")
    task_type: AgentTaskType
    dependencies: tuple[str, ...] = ()
    input_data: dict[str, Any] = Field(default_factory=dict)


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[WorkflowTaskSpec, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowPlan":
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique")
        known = set(task_ids)
        for task in self.tasks:
            if task.task_id in task.dependencies:
                raise ValueError("workflow dependency cycle detected")
            if unknown := set(task.dependencies) - known:
                raise ValueError(f"unknown task dependencies: {sorted(unknown)}")

        graph = {task.task_id: task.dependencies for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("workflow dependency cycle detected")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in graph[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


class AgentRunView(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: int
    user_id: int
    request_text: str
    workflow_kind: WorkflowKind
    status: AgentRunStatus
    plan: dict[str, Any]
    budget: dict[str, Any]
    usage: dict[str, Any]
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class AgentTaskView(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: int
    run_id: int
    task_key: str
    task_type: AgentTaskType
    status: AgentTaskStatus
    dependencies: list[str]
    input_data: dict[str, Any]
    attempts: int
    retryable: bool
    error_code: str | None


class AgentArtifactView(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: int
    run_id: int
    task_id: int
    artifact_kind: str
    payload: dict[str, Any]
    source_hashes: list[str]
    validation_state: ArtifactValidationState
    created_at: datetime
