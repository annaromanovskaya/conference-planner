import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conference_planner.conferences.scheduled_tools import run_scheduled_agent_tools
from conference_planner.conferences.research import (
    CandidateExpiredError,
    CandidateUnusableError,
    ConferenceResearchService,
)
from conference_planner.db.repositories.agent_workflows import AgentWorkflowRepository
from conference_planner.db.repositories.topics import TopicRepository
from conference_planner.runtime_clock import ApplicationClock
from conference_planner.schemas.common import (
    AgentRunStatus,
    AgentTaskType,
    ArtifactValidationState,
    CandidateDecisionReason,
    CandidateStatus,
    WorkflowKind,
)
from conference_planner.schemas.workflows import WorkflowBudget
from conference_planner.workflows.engine import ProgressSink, TaskArtifact, WorkflowEngine
from conference_planner.workflows.planner import WorkflowPlanner

_LEGACY_SEARCH_ERROR_CODES = {"SearchProviderError"}


def _is_search_error(error: str) -> bool:
    return error.startswith("SEARCH_") or error in _LEGACY_SEARCH_ERROR_CODES


class CycleMode(StrEnum):
    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"


class CycleRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: CycleMode
    query: str | None = None
    page_limit: int = Field(default=5, ge=1, le=100)
    results_per_page: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def manual_query_is_required(self) -> "CycleRequest":
        if self.mode == CycleMode.MANUAL and not (self.query or "").strip():
            raise ValueError("manual conference cycle requires a query")
        return self


class ConferenceCycleReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    status: AgentRunStatus
    checked_tracked: int = 0
    updated_tracked: int = 0
    reactivated_candidates: int = 0
    new_candidate_ids: tuple[int, ...] = ()
    duplicates: int = 0
    pages_checked: int = 0
    search_attempts: int = 0
    unresponsive_engines: tuple[str, ...] = ()
    monitoring_errors: tuple[str, ...] = ()
    search_errors: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class MonitoringPort(Protocol):
    async def run(self, user_id: int, *, force: bool = False): ...


@dataclass(frozen=True, slots=True)
class _CycleContext:
    user_id: int
    request: CycleRequest
    queries: tuple[str, ...]


class ConferenceCycleService:
    _inflight: ClassVar[
        dict[tuple[str, int], asyncio.Task[ConferenceCycleReport]]
    ] = {}

    def __init__(
        self,
        repository: AgentWorkflowRepository,
        monitoring: MonitoringPort,
        research: ConferenceResearchService,
        *,
        application_clock: ApplicationClock,
        topics: TopicRepository | None = None,
    ) -> None:
        self.repository = repository
        self.monitoring = monitoring
        self.research = research
        self.application_clock = application_clock
        self.topics = topics or TopicRepository(repository.session)

    async def run(
        self,
        user_id: int,
        request: CycleRequest,
        progress: ProgressSink | None = None,
    ) -> ConferenceCycleReport:
        key = (str(self.repository.session.get_bind().url), user_id)
        current = self._inflight.get(key)
        if current is not None and not current.done():
            return await current
        task = asyncio.create_task(self._run_once(user_id, request, progress))
        self._inflight[key] = task
        try:
            return await task
        finally:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _run_once(
        self,
        user_id: int,
        request: CycleRequest,
        progress: ProgressSink | None,
    ) -> ConferenceCycleReport:
        queries = await self._resolve_queries(user_id, request)
        request_summary = "; ".join(queries)
        context = _CycleContext(user_id, request, queries)
        handlers = self._handlers(context)
        engine = WorkflowEngine(self.repository, handlers)
        active = await self.repository.find_active_run(
            user_id, WorkflowKind.CONFERENCE_CYCLE
        )
        if active is not None:
            run = await engine.resume(active.id, progress)
        else:
            run = await engine.start(
                user_id,
                request_summary,
                WorkflowPlanner.conference_cycle_plan(request_summary),
                progress,
                budget=WorkflowBudget(
                    max_tasks=4,
                    max_pages=max(20, request.page_limit * request.results_per_page),
                    timeout_seconds=3_600,
                ),
                workflow_kind=WorkflowKind.CONFERENCE_CYCLE,
            )
        artifacts = await self.repository.list_artifacts(run.id)
        result = next(
            (
                item.payload
                for item in reversed(artifacts)
                if item.artifact_kind == "conference_cycle_report"
            ),
            {},
        )
        errors = tuple(result.get("errors", ()))
        search_errors = tuple(
            result.get(
                "search_errors",
                (error for error in errors if _is_search_error(error)),
            )
        )
        monitoring_errors = tuple(
            result.get(
                "monitoring_errors",
                (error for error in errors if not _is_search_error(error)),
            )
        )
        return ConferenceCycleReport(
            run_id=run.id,
            status=run.status,
            checked_tracked=result.get("checked_tracked", 0),
            updated_tracked=result.get("updated_tracked", 0),
            reactivated_candidates=result.get("reactivated_candidates", 0),
            new_candidate_ids=tuple(result.get("new_candidate_ids", ())),
            duplicates=result.get("duplicates", 0),
            pages_checked=result.get("pages_checked", 0),
            search_attempts=result.get("search_attempts", 0),
            unresponsive_engines=tuple(result.get("unresponsive_engines", ())),
            monitoring_errors=monitoring_errors,
            search_errors=search_errors,
            errors=errors,
        )

    def _handlers(self, cycle: _CycleContext):
        async def check_tracked(_task_context) -> TaskArtifact:
            try:
                report = await self.monitoring.run(
                    cycle.user_id,
                    force=cycle.request.mode == CycleMode.MANUAL,
                )
            except Exception:
                monitoring_errors = ["TRACKED_CHECK_FAILED"]
                payload = {
                    "checked_tracked": 0,
                    "updated_tracked": 0,
                    "monitoring_errors": monitoring_errors,
                    "errors": monitoring_errors,
                }
            else:
                groups = (
                    report.created,
                    report.changed,
                    report.conflicts,
                    report.unchanged,
                    report.errors,
                    report.catalogs,
                )
                monitoring_errors = [
                    getattr(item, "error_code", "TRACKED_CHECK_FAILED")
                    for item in report.errors
                ]
                payload = {
                    "checked_tracked": sum(len(group) for group in groups),
                    "updated_tracked": len(report.created)
                    + len(report.changed)
                    + len(report.catalogs),
                    "monitoring_errors": monitoring_errors,
                    "errors": monitoring_errors,
                }
            return TaskArtifact(
                artifact_kind="tracked_conference_check",
                payload=payload,
                validation_state=ArtifactValidationState.ACCEPTED,
            )

        async def recheck_decisions(_task_context) -> TaskArtifact:
            values = await self.research.repository.list_candidates_by_statuses(
                cycle.user_id,
                [CandidateStatus.SKIPPED, CandidateStatus.REJECTED],
                limit=100,
            )
            reactivated = 0
            errors: list[str] = []
            for candidate in values:
                if candidate.status != CandidateStatus.SKIPPED:
                    continue
                if candidate.decision_reason not in {
                    CandidateDecisionReason.USER_DEFERRED,
                    CandidateDecisionReason.LOW_RELEVANCE,
                }:
                    continue
                next_review_at = getattr(candidate, "next_review_at", None)
                if (
                    cycle.request.mode == CycleMode.SCHEDULED
                    and next_review_at is not None
                    and next_review_at > self.application_clock.now()
                ):
                    continue
                try:
                    changed = await self.research.recheck_skipped(
                        cycle.user_id, candidate.id
                    )
                except (CandidateExpiredError, CandidateUnusableError):
                    continue
                except Exception as exc:
                    errors.append(f"SEARCH_FAILED:{type(exc).__name__}")
                    continue
                reactivated += int(changed)
            return TaskArtifact(
                artifact_kind="candidate_decision_check",
                payload={
                    "reactivated_candidates": reactivated,
                    "monitoring_errors": errors,
                    "errors": errors,
                },
                validation_state=ArtifactValidationState.ACCEPTED,
            )

        async def search_new(_task_context) -> TaskArtifact:
            new_candidate_ids: list[int] = []
            duplicates = 0
            pages_checked = 0
            search_attempts = 0
            unresponsive_engines: list[str] = []
            errors: list[str] = []
            for query in cycle.queries:
                try:
                    report = await self.research.search_fresh(
                        cycle.user_id,
                        query,
                        page_limit=cycle.request.page_limit,
                        results_per_page=cycle.request.results_per_page,
                    )
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    continue
                for candidate_id in report.new_ids:
                    if candidate_id not in new_candidate_ids:
                        new_candidate_ids.append(candidate_id)
                duplicates += report.duplicates
                pages_checked += report.pages_checked
                search_attempts += report.search_attempts
                if report.error_code is not None:
                    errors.append(report.error_code)
                for diagnostic in report.unresponsive_engines:
                    if diagnostic not in unresponsive_engines:
                        unresponsive_engines.append(diagnostic)
            # The scheduled workflow invokes the same verified research tools as
            # the interactive MCP agent. It is bounded and does not auto-persist.
            tool_statistics = {}
            if cycle.request.mode == CycleMode.SCHEDULED:
                try:
                    year = self.application_clock.today().year + 1
                    topic = cycle.queries[0] if cycle.queries else "astronomy"
                    extra = await run_scheduled_agent_tools(
                        self.research, cycle.user_id, year, topic,
                        new_candidate_ids, list(cycle.queries),
                    )
                    for item in extra.added_ids:
                        if item not in new_candidate_ids:
                            new_candidate_ids.append(item)
                    search_attempts += extra.attempts
                    pages_checked += extra.checked_pages
                    errors.extend(extra.errors)
                    tool_statistics = {
                        "sites_verified": extra.checked_pages,
                        "catalogs_expanded": extra.expanded_catalogs,
                        "deadlines_reviewed": extra.checked_deadlines,
                        "potential_duplicates": extra.potential_duplicates,
                    }
                except Exception as exc:
                    await self.repository.session.rollback()
                    errors.append(f"SCHEDULED_TOOLS:{type(exc).__name__}")
            payload = {
                **tool_statistics,
                "new_candidate_ids": new_candidate_ids,
                "duplicates": duplicates,
                "pages_checked": pages_checked,
                "search_attempts": search_attempts,
                "unresponsive_engines": unresponsive_engines,
                "search_errors": errors,
                "errors": errors,
            }
            return TaskArtifact(
                artifact_kind="fresh_candidate_search",
                payload=payload,
                validation_state=ArtifactValidationState.ACCEPTED,
            )

        async def synthesize(task_context) -> TaskArtifact:
            dependencies = list(task_context.dependencies.values())
            payloads = [item.payload for item in dependencies]
            monitoring_errors: list[str] = []
            search_errors: list[str] = []
            for item in dependencies:
                item_errors = list(item.payload.get("errors", []))
                if "monitoring_errors" in item.payload:
                    monitoring_errors.extend(item.payload["monitoring_errors"])
                elif item.artifact_kind != "fresh_candidate_search":
                    monitoring_errors.extend(item_errors)
                if "search_errors" in item.payload:
                    search_errors.extend(item.payload["search_errors"])
                elif item.artifact_kind == "fresh_candidate_search":
                    search_errors.extend(item_errors)
            payload = {
                "checked_tracked": sum(
                    item.get("checked_tracked", 0) for item in payloads
                ),
                "updated_tracked": sum(
                    item.get("updated_tracked", 0) for item in payloads
                ),
                "reactivated_candidates": sum(
                    item.get("reactivated_candidates", 0) for item in payloads
                ),
                "new_candidate_ids": next(
                    (
                        item["new_candidate_ids"]
                        for item in payloads
                        if "new_candidate_ids" in item
                    ),
                    [],
                ),
                "duplicates": sum(item.get("duplicates", 0) for item in payloads),
                "pages_checked": sum(
                    item.get("pages_checked", 0) for item in payloads
                ),
                "search_attempts": sum(
                    item.get("search_attempts", 0) for item in payloads
                ),
                "unresponsive_engines": list(
                    dict.fromkeys(
                        diagnostic
                        for item in payloads
                        for diagnostic in item.get("unresponsive_engines", [])
                    )
                ),
                "monitoring_errors": monitoring_errors,
                "search_errors": search_errors,
                "errors": [
                    error for item in payloads for error in item.get("errors", [])
                ],
            }
            return TaskArtifact(
                artifact_kind="conference_cycle_report",
                payload=payload,
                validation_state=ArtifactValidationState.ACCEPTED,
            )

        return {
            AgentTaskType.CHECK_TRACKED_CONFERENCES: check_tracked,
            AgentTaskType.RECHECK_SKIPPED_CANDIDATES: recheck_decisions,
            AgentTaskType.SEARCH_CANDIDATES: search_new,
            AgentTaskType.SYNTHESIZE_RESPONSE: synthesize,
        }

    async def _resolve_queries(
        self, user_id: int, request: CycleRequest
    ) -> tuple[str, ...]:
        requested_query = (request.query or "").strip()
        topics = await self.topics.list_for_user(user_id)
        if requested_query and not self._references_saved_topics(requested_query):
            return (requested_query,)
        year = self.application_clock.today().year
        topic_queries = tuple(
            self._topic_search_query(year, topic.name, tuple(topic.keywords))
            for topic in topics
            if topic.enabled
        )
        if topic_queries:
            return tuple(dict.fromkeys(topic_queries))
        return (requested_query or f"Научные конференции {year}-{year + 1}",)

    @staticmethod
    def _topic_search_query(
        year: int,
        name: str,
        keywords: tuple[str, ...],
        *,
        max_chars: int = 240,
    ) -> str:
        prefix = f"Научные конференции {year}-{year + 1}: "
        terms: list[str] = []
        for term in dict.fromkeys((name, *keywords)):
            value = term.strip()
            if not value:
                continue
            candidate = "; ".join((*terms, value))
            if terms and len(prefix) + len(candidate) > max_chars:
                break
            terms.append(value)
        return prefix + "; ".join(terms)

    @staticmethod
    def _references_saved_topics(query: str) -> bool:
        return bool(
            re.search(
                r"\b(мо(?:им|их|и)\s+тем\w*|мо(?:им|их|и)\s+интерес\w*|"
                r"my\s+(?:topics|interests))\b",
                query.casefold(),
            )
        )
