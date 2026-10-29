import asyncio
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conference_planner.agent.actions import ActionUnavailable, ConfirmationService
from conference_planner.agent.knowledge import KnowledgeGraphService
from conference_planner.agent.memory import MemoryService
from conference_planner.agent.runtime import AgentRuntime
from conference_planner.ai.active_provider import ActiveLLMProvider
from conference_planner.ai.registry import ModelRegistry, ProviderProfile, build_provider_profiles
from conference_planner.ai.service import AIService
from conference_planner.ai.usage import OllamaUsageService
from conference_planner.bot.agent_router import format_cycle_report
from conference_planner.bot.dependencies import RepositoryMiddleware
from conference_planner.bot.router import build_router
from conference_planner.bot.discovery_progress import DiscoveryProgressController
from conference_planner.bot.usage_router import CycleScheduleService
from conference_planner.conferences.cycle import (
    ConferenceCycleReport,
    ConferenceCycleService,
    CycleMode,
    CycleRequest,
)
from conference_planner.conferences.deduplication import normalize_name
from conference_planner.conferences.discovery import DiscoveryService
from conference_planner.conferences.evidence import EvidenceDocument, EvidenceValidator
from conference_planner.conferences.monitoring import (
    CatalogConversion,
    MonitoringCheckError,
    MonitoringService,
)
from conference_planner.conferences.persistence import ConferencePersistenceService
from conference_planner.conferences.research import ConferenceResearchService
from conference_planner.config import Settings
from conference_planner.db.migrations import run_migrations
from conference_planner.db.models import (
    Conference,
    ConferenceChange,
    ConferenceEvidence,
    ConferenceTopic,
    EventCandidate,
    Subscription,
    Topic,
    DiscoveryJob,
)
from conference_planner.db.repositories.agent_state import AgentStateRepository
from conference_planner.db.repositories.agent_workflows import AgentWorkflowRepository
from conference_planner.db.repositories.conference_research import ConferenceResearchRepository
from conference_planner.db.repositories.conferences import ConferenceRepository
from conference_planner.db.repositories.memories import MemoryRepository
from conference_planner.db.repositories.ollama_usage import OllamaUsageRepository
from conference_planner.db.repositories.parser_recipes import ParserRecipeRepository
from conference_planner.db.repositories.reminders import ReminderRepository
from conference_planner.db.repositories.sources import SourceRepository
from conference_planner.db.repositories.test_reminders import TestReminderRepository
from conference_planner.db.repositories.topics import TopicRepository
from conference_planner.db.repositories.web_cache import WebDocumentCacheRepository
from conference_planner.db.session import create_engine_and_sessionmaker
from conference_planner.mcp.client import StreamableHTTPToolClient
from conference_planner.mcp.lifecycle import MCPServerLifecycle
from conference_planner.mcp.server import build_mcp_app, build_mcp_server
from conference_planner.mcp.tools import MCPToolServices
from conference_planner.owner import OwnerContext, bootstrap_owner
from conference_planner.reminders.service import ReminderNotice, ReminderService
from conference_planner.reminders.test_service import TestReminderService
from conference_planner.runtime_clock import ApplicationClock
from conference_planner.scheduler.setup import configure_scheduler
from conference_planner.schemas.agent import (
    AddSourceAction,
    AddTopicAction,
    ArchiveConferenceAction,
    DeleteConferenceAction,
    DeleteSourceAction,
    DeleteTopicAction,
    ForgetMemoryAction,
    ImportConferenceCandidateAction,
    ImportConferenceURLAction,
    MemoryProposal,
    RunDiscoveryAction,
    SaveMemoryAction,
    SetCandidateDecisionAction,
    TrackConferenceAction,
    UpsertDeadlineAction,
)
from conference_planner.schemas.common import (
    AgentRunStatus,
    AgentTaskType,
    ArtifactValidationState,
    CandidateDecisionReason,
    CandidateDecisionSource,
    CandidateStatus,
    ConferenceStatus,
    EvidenceSourceClass,
    ModelRole,
    VerificationStatus,
    WorkflowKind,
)
from conference_planner.schemas.research import (
    ConferenceResearch,
    DeadlineClaim,
)
from conference_planner.schemas.topics import TopicNormalization
from conference_planner.schemas.workflows import (
    AgentRunView,
    WorkflowBudget,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from conference_planner.search.searxng import SearXNGSearchProvider
from conference_planner.web.fetcher import PageFetcher, normalized_content_hash
from conference_planner.web.parser import extract_page_text
from conference_planner.web.reader import JinaPageReader
from conference_planner.web.url_safety import URLSafetyPolicy
from conference_planner.workflows.engine import (
    TaskArtifact,
    WorkflowEngine,
    WorkflowTaskFailure,
)
from conference_planner.workflows.planner import WorkflowPlanner
from conference_planner.workflows.progress import WorkflowProgress

logger = logging.getLogger(__name__)


def _reader_fallback(settings: Settings, fetcher: PageFetcher) -> JinaPageReader | None:
    if settings.reader_fallback_url is None:
        return None
    return JinaPageReader(fetcher.client, str(settings.reader_fallback_url))


def _model_registry(
    session: AsyncSession,
    profiles: Sequence[ProviderProfile],
    *,
    capability_timeout: float = 20,
    settings: Settings | None = None,
) -> ModelRegistry:
    return ModelRegistry(
        AgentStateRepository(session),
        profiles,
        usage_repository=OllamaUsageRepository(session),
        capability_timeout=capability_timeout,
        role_models=settings.model_role_overrides if settings is not None else None,
    )


def _research_ai_service(registry: ModelRegistry, user_id: int) -> AIService:
    return AIService(
        ActiveLLMProvider(registry, user_id, ModelRole.RESEARCH),
        verifier_provider=ActiveLLMProvider(registry, user_id, ModelRole.VERIFIER),
    )


def _conference_cycle_service(
    session: AsyncSession,
    user_id: int,
    settings: Settings,
    registry: ModelRegistry,
    search,
    fetcher,
    application_clock: ApplicationClock,
) -> ConferenceCycleService:
    ai = _research_ai_service(registry, user_id)
    cache = WebDocumentCacheRepository(session)
    research = ConferenceResearchService(
        session,
        search,
        fetcher,
        ai,
        max_page_bytes=settings.max_page_bytes,
        max_page_chars=settings.max_page_text_chars,
        application_clock=application_clock,
        min_relevance_score=settings.min_relevance_score,
        cache=cache,
    )
    monitoring = MonitoringService(
        session,
        fetcher,
        ai,
        settings.max_page_bytes,
        settings.max_page_text_chars,
        search,
        cache=cache,
        recipes=ParserRecipeRepository(session),
        reader=_reader_fallback(settings, fetcher),
        monitoring_interval_hours=settings.monitoring_interval_hours,
    )
    return ConferenceCycleService(
        AgentWorkflowRepository(session),
        monitoring,
        research,
        application_clock=application_clock,
        topics=TopicRepository(session),
    )


def _durable_conference_workflow(
    session: AsyncSession,
    user_id: int,
    settings: Settings,
    profiles: Sequence[ProviderProfile],
    search,
    fetcher,
    application_clock: ApplicationClock,
    on_discovery_progress=None,
    discovery_job_id: int | None = None,
) -> tuple[WorkflowEngine, WorkflowProgress]:
    registry = _model_registry(session, profiles, settings=settings)
    ai = _research_ai_service(registry, user_id)
    research = ConferenceResearchService(
        session,
        search,
        fetcher,
        ai,
        max_page_bytes=settings.max_page_bytes,
        max_page_chars=settings.max_page_text_chars,
        application_clock=application_clock,
        min_relevance_score=settings.min_relevance_score,
        discovery_job_id=discovery_job_id,
    )
    async def search_task(context):
        years = context.task.input_data.get("years")
        if isinstance(years, list) and all(isinstance(year, int) for year in years):
            service = _discovery_service(
                session, user_id, settings, registry, search, fetcher
            )
            report = await service.run(user_id, years=years, on_progress=on_discovery_progress)
            return TaskArtifact(
                artifact_kind="candidate_list",
                payload={"candidate_ids": list(report.created_ids)},
            )
        query = str(context.task.input_data.get("query", context.run.request_text))
        # User-configured sources are a first-class discovery channel and are
        # fetched directly even when every SearXNG engine is unavailable.
        trusted_report = await research.search_trusted_sources(user_id, query)
        try:
            search_report = await research.search_fresh(
                user_id, query, page_limit=5, results_per_page=20,
            )
        except Exception as exc:
            # A failed external search must not discard results from the
            # independent configured-source crawler.
            await session.rollback()
            from types import SimpleNamespace
            search_report = SimpleNamespace(
                active_ids=(), new_ids=(), search_attempts=0,
                duplicates=0, error_code="SEARCH_UNAVAILABLE",
            )
            logger.warning("SearXNG search failed after trusted source crawl: %s", type(exc).__name__)
        direct_ids = list(trusted_report["new_ids"])
        combined_new_ids = list(dict.fromkeys([*direct_ids, *search_report.new_ids]))
        combined_active_ids = list(dict.fromkeys([
            *trusted_report["active_ids"], *search_report.active_ids,
        ]))
        # Revisit candidates already discovered in earlier runs. Only-new
        # selection caused subsequent searches to inspect zero pages.
        candidates_for_check = list(await session.scalars(
            select(EventCandidate).where(
                EventCandidate.user_id == user_id,
                EventCandidate.id.in_(combined_active_ids),
                EventCandidate.status == CandidateStatus.ACTIVE,
                EventCandidate.researched_at.is_(None),
            )
        )) if combined_active_ids else []
        requested_years = set(re.findall(r"\b20\d{2}\b", query))
        def priority(item):
            from urllib.parse import urlsplit
            text = f"{item.title} {item.source_url}".casefold()
            url = urlsplit(item.source_url)
            year_matches = bool(requested_years & set(re.findall(r"20\d{2}", text)))
            has_other_year = bool(set(re.findall(r"20\d{2}", text)) - requested_years)
            official = any(domain in (url.hostname or "") for domain in (
                "iau.org", "eso.org", "aas.org", "indico.cern.ch",
                "sai.msu.ru", "inasan.ru", "esa.int",
            ))
            document = bool(re.search(r"\.(?:pdf|docx?|xlsx?)(?:$|[?#])", item.source_url, re.I))
            return (document, has_other_year and not year_matches,
                    not year_matches, not official, -(item.relevance_score or 0), item.id)
        candidates_for_check.sort(key=priority)
        inspection_ids = [item.id for item in candidates_for_check[:12]]
        # Research bounded candidate pages now, rather than treating SearXNG
        # snippets as checked conference sites. Record URL-level evidence.
        verified_pages = 0
        verification_errors = 0
        verified_candidate_ids = []
        model_errors = 0
        if discovery_job_id is not None:
            from conference_planner.db.models import DiscoverySearchAudit
            from conference_planner.conferences.research import CatalogExpansion
            for candidate_id in inspection_ids:
                candidate = await research.repository.get_candidate(user_id, candidate_id)
                url = candidate.source_url
                try:
                    result = await research.inspect(user_id, candidate_id)
                    checked = not isinstance(result, CatalogExpansion)
                    status = "RESEARCHED" if checked else "CATALOG_EXPANDED"
                    verified_pages += 1  # successful URL inspection, including expanded catalogs
                    if checked:
                        verified_candidate_ids.append(candidate_id)
                except Exception as exc:
                    verification_errors += 1
                    if type(exc).__name__ == "ModelGatewayError":
                        model_errors += 1
                    status = "INSPECTION_FAILED"
                    diagnostic = type(exc).__name__
                    # An exception can leave SQLAlchemy in an invalid transaction.
                    await session.rollback()
                else:
                    diagnostic = None
                session.add(DiscoverySearchAudit(
                    job_id=discovery_job_id, query="[candidate page]", page=1,
                    result_count=0, retained_count=0, ignored_count=0,
                    status=status, diagnostics=[diagnostic] if diagnostic else [],
                    urls=[{"url": url, "decision": status}],
                ))
                await session.commit()
        if on_discovery_progress is not None:
            # The progress callback opens an independent session: release any
            # search transaction before attempting to update its job row.
            await session.commit()
            from types import SimpleNamespace
            await on_discovery_progress(SimpleNamespace(
                created_ids=combined_new_ids,
                pages_checked=verified_pages + trusted_report["sources_opened"],
                search_attempts=search_report.search_attempts,
                rejected_urls=[],
                errors=([search_report.error_code] if search_report.error_code and not trusted_report["sources_opened"] else []) + list(trusted_report["errors"]) + ["INSPECTION_FAILED"] * verification_errors,
            ), query)
        return TaskArtifact(
            artifact_kind="candidate_list",
            payload={"candidate_ids": combined_active_ids,
                     "new_ids": combined_new_ids,
                     "search_attempts": search_report.search_attempts,
                     "verified_pages": verified_pages,
                     "verified_candidate_ids": verified_candidate_ids,
                     "model_errors": model_errors,
                     "pending_verification": max(0, len(candidates_for_check)-len(inspection_ids)),
                     "trusted_sources_opened": trusted_report["sources_opened"],
                     "verification_errors": verification_errors,
                     "duplicates": search_report.duplicates,
                     "error_code": (search_report.error_code if not trusted_report["sources_opened"] and not combined_new_ids
                                    else "PARTIAL_VERIFICATION" if verification_errors or trusted_report["pending_catalogs"]
                                    else "VERIFICATION_BUDGET_REACHED" if len(candidates_for_check) > len(inspection_ids)
                                    else "NO_VERIFIED_EVENTS" if not verified_candidate_ids else None)},
        )

    async def crawl_task(context):
        source_url = context.task.input_data.get("url")
        candidate_id = None
        if not source_url and "search" in context.dependencies:
            ids = context.dependencies["search"].payload.get("candidate_ids", [])
            if not ids:
                raise WorkflowTaskFailure("NO_CANDIDATES", retryable=False)
            candidate_id = int(ids[0])
            source_url = (await research.get_candidate(user_id, candidate_id)).source_url
        if not isinstance(source_url, str):
            raise WorkflowTaskFailure("SOURCE_URL_MISSING", retryable=False)
        page = await fetcher.fetch(source_url, max_bytes=settings.max_page_bytes)
        text = extract_page_text(page.html, max_chars=settings.max_page_text_chars)
        return TaskArtifact(
            artifact_kind="evidence_document",
            payload={"url": page.url, "text": text, "candidate_id": candidate_id},
            source_hashes=(page.content_hash or normalized_content_hash(page.html),),
            pages=1,
            characters=len(text),
        )

    async def extract_task(context):
        document = context.dependencies["crawl"].payload
        result = await ai.extract_conference_research(
            document["url"], document["text"], context.run.request_text
        )
        return TaskArtifact(
            artifact_kind="conference_research",
            payload={"research": result.model_dump(mode="json"), "document": document},
            source_hashes=tuple(context.dependencies["crawl"].source_hashes),
            model_calls=1,
        )

    async def verify_task(context):
        extracted = context.dependencies["extract"].payload
        document = extracted["document"]
        verified = await ai.verify_conference_research(
            ConferenceResearch.model_validate(extracted["research"]),
            evidence_documents=((document["url"], document["text"]),),
        )
        validation = EvidenceValidator().validate(
            verified, (EvidenceDocument(document["url"], document["text"]),)
        )
        if not validation.validated.facts and not validation.validated.deadlines:
            raise WorkflowTaskFailure("NO_VERIFIED_FACTS", retryable=False)
        return TaskArtifact(
            artifact_kind="verified_conference",
            payload={
                "research": validation.validated.model_dump(mode="json"),
                "candidate_id": document.get("candidate_id"),
            },
            source_hashes=tuple(context.dependencies["extract"].source_hashes),
            validation_state=ArtifactValidationState.ACCEPTED,
            model_calls=1,
        )

    async def propose_task(context):
        verified = context.dependencies["verify"]
        return TaskArtifact(
            artifact_kind="conference_candidate",
            payload=verified.payload,
            source_hashes=tuple(verified.source_hashes),
            validation_state=verified.validation_state,
        )

    repository = AgentWorkflowRepository(session)
    return (
        WorkflowEngine(
            repository,
            {
                AgentTaskType.SEARCH_CANDIDATES: search_task,
                AgentTaskType.CRAWL_CANDIDATE: crawl_task,
                AgentTaskType.EXTRACT_RESEARCH: extract_task,
                AgentTaskType.VERIFY_RESEARCH: verify_task,
                AgentTaskType.PROPOSE_PERSISTENCE: propose_task,
            },
        ),
        WorkflowProgress(repository),
    )


async def _run_scheduled_discovery(
    session: AsyncSession,
    user_id: int,
    discovery_service,
    *,
    years: list[int],
) -> tuple[int, ...]:
    async def discover_task(_context):
        report = await discovery_service.run(user_id, years=years)
        return TaskArtifact(
            artifact_kind="candidate_list",
            payload={"candidate_ids": list(report.created_ids)},
        )

    repository = AgentWorkflowRepository(session)
    plan = WorkflowPlan(
        tasks=(
            WorkflowTaskSpec(
                task_id="scheduled_discovery",
                task_type=AgentTaskType.SEARCH_CANDIDATES,
                input_data={"years": years},
            ),
        )
    )
    run = await WorkflowEngine(
        repository, {AgentTaskType.SEARCH_CANDIDATES: discover_task}
    ).start(
        user_id,
        f"Фоновый поиск конференций за {', '.join(map(str, years))}",
        plan,
        budget=WorkflowBudget(max_tasks=1),
    )
    artifacts = await repository.list_artifacts(run.id)
    if not artifacts:
        return ()
    return tuple(int(item) for item in artifacts[-1].payload.get("candidate_ids", []))


async def _run_scheduled_conference_cycle(
    user_id: int,
    conference_cycle_service: ConferenceCycleService,
) -> ConferenceCycleReport:
    return await conference_cycle_service.run(
        user_id,
        CycleRequest(mode=CycleMode.SCHEDULED),
    )


class _MCPConfirmationProxy:
    def __init__(self, session_factory, owner_id: int) -> None:
        self.session_factory = session_factory
        self.owner_id = owner_id

    async def propose(self, user_id: int, payload):
        if user_id != self.owner_id:
            raise ValueError("owner mismatch")
        async with self.session_factory() as session:
            return await ConfirmationService(session, executors={}).propose(user_id, payload)


async def run_application(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await asyncio.to_thread(run_migrations, settings.database_url)
    application_clock = ApplicationClock.start(settings.timezone)
    engine, session_factory = create_engine_and_sessionmaker(settings.database_url)
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        async with session_factory() as session:
            owner = await bootstrap_owner(session, settings.owner_telegram_id)

        timeout = httpx.Timeout(settings.request_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": "ConfPlanner/0.1"}
        ) as web_client:
            profiles = build_provider_profiles(settings, web_client)
            fetcher = PageFetcher(web_client, URLSafetyPolicy())
            search = SearXNGSearchProvider(str(settings.searxng_url), web_client)
            read_handlers = _build_read_handlers(
                session_factory,
                owner,
                profiles,
                search=search,
                fetcher=fetcher,
                settings=settings,
                application_clock=application_clock,
            )
            mcp_server = build_mcp_server(
                MCPToolServices(
                    owner_id=owner.user_id,
                    read_handlers=read_handlers,
                    confirmation=_MCPConfirmationProxy(session_factory, owner.user_id),
                    max_result_chars=settings.agent_max_tool_result_chars,
                )
            )
            mcp_app = build_mcp_app(mcp_server, host=settings.mcp_host, port=settings.mcp_port)
            mcp_url = f"http://{settings.mcp_host}:{settings.mcp_port}/mcp"

            async with MCPServerLifecycle(mcp_app, host=settings.mcp_host, port=settings.mcp_port):
                async with StreamableHTTPToolClient(
                    mcp_url,
                    timeout_seconds=settings.agent_timeout_seconds,
                    max_result_chars=settings.agent_max_tool_result_chars,
                ) as tool_client:
                    await _run_bot(
                        settings,
                        bot,
                        session_factory,
                        owner,
                        profiles,
                        tool_client,
                        search,
                        fetcher,
                        application_clock,
                    )
    finally:
        await bot.session.close()
        await engine.dispose()


async def _run_bot(
    settings: Settings,
    bot: Bot,
    session_factory,
    owner: OwnerContext,
    profiles: tuple[ProviderProfile, ...],
    tool_client,
    search,
    fetcher,
    application_clock: ApplicationClock,
) -> None:
    dispatcher = Dispatcher(storage=MemoryStorage())
    discovery_controller = DiscoveryProgressController(
        bot, session_factory, owner.user_id, owner.telegram_id,
        settings, profiles, search, fetcher, application_clock,
    )
    router = build_router(owner_telegram_id=owner.telegram_id)

    def services(session: AsyncSession) -> dict[str, Any]:
        registry = _model_registry(
            session,
            profiles,
            capability_timeout=min(20, settings.agent_timeout_seconds),
            settings=settings,
        )
        memory = MemoryService(session, max_context_chars=settings.agent_max_context_chars)
        confirmation = ConfirmationService(
            session,
            executors=_build_action_executors(
                session,
                owner.user_id,
                settings,
                registry,
                search,
                fetcher,
                application_clock,
                enqueue_discovery=discovery_controller.enqueue_callback(session),
            ),
        )
        ai_service = AIService(ActiveLLMProvider(registry, owner.user_id))
        research_ai_service = _research_ai_service(registry, owner.user_id)
        workflow_engine = None
        workflow_planner = None
        if settings.agent_workflows_enabled:
            workflow_engine, _progress = _durable_conference_workflow(
                session,
                owner.user_id,
                settings,
                profiles,
                search,
                fetcher,
                application_clock,
            )
            workflow_planner = WorkflowPlanner()
        conference_research = ConferenceResearchService(
            session,
            search,
            fetcher,
            research_ai_service,
            max_page_bytes=settings.max_page_bytes,
            max_page_chars=settings.max_page_text_chars,
            application_clock=application_clock,
            min_relevance_score=settings.min_relevance_score,
        )
        monitoring = MonitoringService(
            session,
            fetcher,
            research_ai_service,
            settings.max_page_bytes,
            settings.max_page_text_chars,
            search,
            cache=WebDocumentCacheRepository(session),
            recipes=ParserRecipeRepository(session),
            reader=_reader_fallback(settings, fetcher),
            monitoring_interval_hours=settings.monitoring_interval_hours,
        )
        return {
            "model_registry": registry,
            "application_clock": application_clock,
            "conference_schedule_service": CycleScheduleService(
                AgentWorkflowRepository(session),
                application_clock,
                settings.discovery_hour,
            ),
            "ollama_usage_service": OllamaUsageService(OllamaUsageRepository(session), registry),
            "memory_service": memory,
            "confirmation_service": confirmation,
            "discovery_controller": discovery_controller,
            "agent_runtime": AgentRuntime(
                registry,
                memory,
                tool_client,
                max_steps=settings.agent_max_steps,
                max_tool_calls_per_step=settings.agent_max_tool_calls_per_step,
                timeout_seconds=settings.agent_timeout_seconds,
                tracked_check_timeout_seconds=settings.tracked_check_timeout_seconds,
                max_tool_result_chars=settings.agent_max_tool_result_chars,
                workflow_planner=workflow_planner,
                workflow_engine=workflow_engine,
                application_clock=application_clock,
            ),
            "ai_service": ai_service,
            "conference_research_service": conference_research,
            "monitoring_service": monitoring,
            "conference_cycle_service": ConferenceCycleService(
                AgentWorkflowRepository(session),
                monitoring,
                conference_research,
                application_clock=application_clock,
                topics=TopicRepository(session),
            ),
        }

    middleware = RepositoryMiddleware(session_factory, owner.user_id, service_factory=services)
    router.message.middleware(middleware)
    router.callback_query.middleware(middleware)
    # Specific discovery callbacks must be registered before the catch-all fallback.
    dispatcher.include_router(discovery_controller.router())
    dispatcher.include_router(router)

    async def conference_cycle_job() -> None:
        async with session_factory() as session:
            registry = _model_registry(session, profiles, settings=settings)
            service = _conference_cycle_service(
                session,
                owner.user_id,
                settings,
                registry,
                search,
                fetcher,
                application_clock,
            )
            report = await _run_scheduled_conference_cycle(owner.user_id, service)
        await bot.send_message(owner.telegram_id, format_cycle_report(report))

    async def reminder_job() -> None:
        async with session_factory() as session:
            repository = ReminderRepository(session)

            async def send(notice: ReminderNotice) -> None:
                await bot.send_message(owner.telegram_id, notice.telegram_text())

            await ReminderService(repository, settings.reminder_days).process_daily(
                owner.user_id, application_clock.today(), send
            )

    async def test_reminder_job() -> None:
        async with session_factory() as session:
            service = TestReminderService(
                TestReminderRepository(session), application_clock.timezone
            )

            async def send(user_id: int, text: str) -> None:
                if user_id != owner.user_id:
                    raise ValueError("test reminder owner mismatch")
                await bot.send_message(owner.telegram_id, text)

            await service.process_due(application_clock.now(), send)

    scheduler = AsyncIOScheduler()

    if settings.agent_workflows_enabled:
        async with session_factory() as session:
            engine, progress = _durable_conference_workflow(
                session,
                owner.user_id,
                settings,
                profiles,
                search,
                fetcher,
                application_clock,
            )
            recovery = await engine.recover_incomplete_runs(
                progress,
                workflow_kind=WorkflowKind.CONFERENCE_RESEARCH,
            )
            cycle_repository = AgentWorkflowRepository(session)
            active_cycle = await cycle_repository.find_active_run(
                owner.user_id, WorkflowKind.CONFERENCE_CYCLE
            )
            cycle_recovered_id = None
            cycle_failed_id = None
            if active_cycle is not None:
                registry = _model_registry(session, profiles, settings=settings)
                cycle = _conference_cycle_service(
                    session,
                    owner.user_id,
                    settings,
                    registry,
                    search,
                    fetcher,
                    application_clock,
                )
                try:
                    cycle_report = await cycle.run(
                        owner.user_id,
                        CycleRequest(mode=CycleMode.SCHEDULED),
                        progress=WorkflowProgress(cycle_repository),
                    )
                except Exception:
                    cycle_failed_id = active_cycle.id
                else:
                    if cycle_report.status == AgentRunStatus.COMPLETED:
                        cycle_recovered_id = active_cycle.id
                    else:
                        cycle_failed_id = active_cycle.id
        recovered_ids = list(recovery.recovered_run_ids)
        failed_ids = list(recovery.failed_run_ids)
        if cycle_recovered_id is not None:
            recovered_ids.append(cycle_recovered_id)
        if cycle_failed_id is not None:
            failed_ids.append(cycle_failed_id)
        if recovered_ids:
            await bot.send_message(
                owner.telegram_id,
                "Восстановлены прерванные задачи: "
                + ", ".join(map(str, recovered_ids)),
            )
        if failed_ids:
            await bot.send_message(
                owner.telegram_id,
                "Не удалось восстановить задачи: "
                + ", ".join(map(str, failed_ids)),
            )
    configure_scheduler(
        scheduler,
        conference_cycle_job=conference_cycle_job,
        reminder_job=reminder_job,
        test_reminder_job=test_reminder_job,
        discovery_hour=settings.discovery_hour,
        discovery_interval_hours=settings.discovery_interval_hours,
        reminder_hour=settings.reminder_hour,
        timezone=settings.app_timezone,
    )
    scheduler.start()
    await discovery_controller.start()
    try:
        await dispatcher.start_polling(bot)
    finally:
        await discovery_controller.stop()
        scheduler.shutdown(wait=False)


def _build_action_executors(
    session,
    owner_id: int,
    settings,
    registry,
    search,
    fetcher,
    application_clock: ApplicationClock | None = None,
    enqueue_discovery=None,
) -> dict[str, Any]:
    async def add_topic(user_id: int, payload: AddTopicAction) -> str:
        description = payload.description.strip() or payload.name
        keywords = [item for item in payload.keywords if item.strip()] or [payload.name]
        await TopicRepository(session).create(
            user_id,
            TopicNormalization(
                name=payload.name,
                description=description,
                keywords=keywords,
            ),
        )
        return "Тема добавлена."

    async def delete_topic(user_id: int, payload: DeleteTopicAction) -> str:
        topic_name = await TopicRepository(session).delete_for_user(user_id, payload.topic_id)
        if topic_name is None:
            raise ActionUnavailable("topic not found")
        return f"Тема «{topic_name}» удалена."

    async def add_source(_user_id: int, payload: AddSourceAction) -> str:
        await SourceRepository(session).create(payload.value, payload.value, payload.source_type)
        return "Источник добавлен."

    async def delete_source(_user_id: int, payload: DeleteSourceAction) -> str:
        source_name = await SourceRepository(session).delete(payload.source_id)
        if source_name is None:
            raise ActionUnavailable("source not found")
        return f"Источник «{source_name}» удалён."

    async def track(user_id: int, payload: TrackConferenceAction) -> str:
        repository = ConferenceRepository(session)
        conference = await repository.require_accessible(user_id, payload.conference_id)
        if conference.is_test:
            raise ValueError("conference not found")
        await repository.track(user_id, payload.conference_id)
        return "Конференция отслеживается."

    async def delete_conference(user_id: int, payload: DeleteConferenceAction) -> str:
        conference_name = await ConferenceRepository(session).remove_for_user(
            user_id, payload.conference_id
        )
        if conference_name is None:
            raise ActionUnavailable("conference not found")
        return f"Конференция «{conference_name}» удалена из ваших списков."

    async def import_candidate(
        user_id: int,
        payload: ImportConferenceCandidateAction | ImportConferenceURLAction,
    ) -> str:
        if isinstance(payload, ImportConferenceCandidateAction):
            receipt = await ConferenceResearchService(
                session,
                search,
                fetcher,
                _research_ai_service(registry, owner_id),
                max_page_bytes=settings.max_page_bytes,
                max_page_chars=settings.max_page_text_chars,
                application_clock=application_clock,
                min_relevance_score=getattr(settings, "min_relevance_score", 0.5),
            ).add(user_id, payload.candidate_id)
            return receipt.telegram_summary()
        service = _discovery_service(session, owner_id, settings, registry, search, fetcher)
        conference_id = await service.import_candidate(
            user_id, str(payload.source_url), expected_name=payload.title
        )
        repository = ConferenceRepository(session)
        await repository.track(user_id, conference_id)
        conference = await repository.require_accessible(user_id, conference_id)
        message = f"Конференция «{conference.name}» сохранена и отслеживается."
        if conference.last_checked_at is None:
            message += " Страница пока не проверена; бот повторит проверку при мониторинге."
        return message

    async def archive(user_id: int, payload: ArchiveConferenceAction) -> str:
        repository = ConferenceRepository(session)
        conference = await repository.require_accessible(user_id, payload.conference_id)
        if conference.is_test:
            raise ValueError("conference not found")
        await repository.archive(user_id, payload.conference_id)
        return "Конференция перемещена в архив."

    async def save_memory(user_id: int, payload: SaveMemoryAction) -> str:
        proposal = MemoryProposal.model_validate(payload.model_dump(exclude={"action"}))
        await MemoryService(
            session, max_context_chars=settings.agent_max_context_chars
        ).save_memory(user_id, proposal)
        return "Запись сохранена в память."

    async def forget_memory(user_id: int, payload: ForgetMemoryAction) -> str:
        memory = await MemoryRepository(session).deactivate(user_id, payload.memory_id)
        if memory is None:
            raise ValueError("memory not found")
        return "Запись удалена из активной памяти."

    async def run_discovery(user_id: int, payload: RunDiscoveryAction) -> str:
        if enqueue_discovery is None:
            raise ActionUnavailable("discovery queue is unavailable")
        job = await enqueue_discovery(user_id, list(payload.years))
        return f"Поиск поставлен в очередь (задание №{job.id})."

    async def upsert_deadline(user_id: int, payload: UpsertDeadlineAction) -> str:
        repository = ConferenceRepository(session)
        conference = await repository.require_accessible(user_id, payload.conference_id)
        if conference.is_test:
            raise ValueError("conference not found")
        source_url = str(payload.source_url) if payload.source_url else (
            conference.website or conference.source_url
        )
        if not source_url:
            raise ValueError("conference has no source URL")
        tentative = payload.tentative or payload.source_url is None
        claim = DeadlineClaim(
            deadline_type=payload.deadline_type,
            exact_date=payload.exact_date,
            approximate_year=payload.approximate_year,
            approximate_month=payload.approximate_month,
            tentative=tentative,
            source_url=source_url,
            source_class=EvidenceSourceClass.SECONDARY,
            excerpt="Дедлайн введён владельцем вручную и требует проверки источника.",
            verification_status=VerificationStatus.TENTATIVE,
        )
        receipt = await ConferencePersistenceService(session).persist_deadline(
            user_id,
            conference.id,
            claim,
        )
        if not receipt.committed:
            raise RuntimeError("deadline persistence did not commit")
        when = (
            payload.exact_date.isoformat()
            if payload.exact_date is not None
            else f"{payload.approximate_year}"
            + (
                f"-{payload.approximate_month:02d}"
                if payload.approximate_month is not None
                else ""
            )
            + " (ориентировочно)"
        )
        field_name = f"deadline:{payload.deadline_type.value}"
        changed = field_name in {*receipt.created_fields, *receipt.updated_fields}
        current = next(
            (
                item
                for item in receipt.deadlines
                if item.deadline_type == payload.deadline_type
            ),
            None,
        )
        desired_is_current = current is not None and (
            (
                payload.exact_date is not None
                and current.exact_date == payload.exact_date
            )
            or (
                payload.exact_date is None
                and current.exact_date is None
                and current.approximate_year == payload.approximate_year
                and current.approximate_month == payload.approximate_month
            )
        )
        if changed:
            outcome = "сохранён"
        elif desired_is_current:
            outcome = "уже был сохранён"
        else:
            outcome = "не изменён: в базе есть значение из более доверенного источника"
        return (
            f"Дедлайн {payload.deadline_type.value}: {when} {outcome} для "
            f"«{conference.name}». Квитанция: conference_id={receipt.conference_id}, "
            "committed=true."
        )

    async def set_candidate_decision(
        user_id: int, payload: SetCandidateDecisionAction
    ) -> str:
        repository = ConferenceResearchRepository(session)
        candidate = await repository.get_candidate(user_id, payload.candidate_id)
        now = application_clock.now() if application_clock is not None else datetime.now(UTC)
        decision = CandidateStatus(payload.decision)
        if decision == CandidateStatus.ACTIVE:
            if candidate.status not in {CandidateStatus.SKIPPED, CandidateStatus.REJECTED}:
                raise ValueError("candidate is not deferred or rejected")
            await repository.transition_candidate(
                user_id,
                candidate.id,
                CandidateStatus.ACTIVE,
                None,
                CandidateDecisionSource.USER,
                now,
            )
            message = f"Кандидат {candidate.id} возвращён в очередь."
        elif decision == CandidateStatus.SKIPPED:
            await repository.transition_candidate(
                user_id,
                candidate.id,
                CandidateStatus.SKIPPED,
                CandidateDecisionReason.USER_DEFERRED,
                CandidateDecisionSource.USER,
                now,
                next_review_at=now + timedelta(days=7),
            )
            message = f"Кандидат {candidate.id} пропущен."
        else:
            await repository.transition_candidate(
                user_id,
                candidate.id,
                CandidateStatus.REJECTED,
                CandidateDecisionReason.USER_REJECTED,
                CandidateDecisionSource.USER,
                now,
            )
            message = f"Кандидат {candidate.id} отклонён."
        await session.commit()
        return message

    return {
        "add_topic": add_topic,
        "delete_topic": delete_topic,
        "add_source": add_source,
        "delete_source": delete_source,
        "track_conference": track,
        "delete_conference": delete_conference,
        "import_conference_candidate": import_candidate,
        "import_conference_url": import_candidate,
        "archive_conference": archive,
        "save_memory": save_memory,
        "forget_memory": forget_memory,
        "run_discovery": run_discovery,
        "upsert_deadline": upsert_deadline,
        "set_candidate_decision": set_candidate_decision,
    }


def _discovery_service(session, owner_id, settings, registry, search, fetcher):
    return DiscoveryService(
        session,
        search,
        fetcher,
        _research_ai_service(registry, owner_id),
        settings.min_relevance_score,
        settings.max_page_bytes,
        settings.max_page_text_chars,
    )


def _build_read_handlers(
    session_factory: async_sessionmaker,
    owner: OwnerContext,
    profiles,
    *,
    search,
    fetcher,
    settings,
    application_clock: ApplicationClock,
) -> dict[str, Any]:
    async def with_session(operation):
        async with session_factory() as session:
            return await operation(session)

    async def list_topics(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            topics = await TopicRepository(session).list_for_user(user_id)
            return {
                "items": [
                    {"kind": "topic", "id": item.id, "name": item.name}
                    for item in topics[: arguments["limit"]]
                ],
                "provenance": {"source": "database"},
            }

        return await with_session(operation)

    async def get_topic(user_id: int, arguments: dict) -> dict | None:
        async def operation(session):
            topic = await TopicRepository(session).get_for_user(user_id, arguments["topic_id"])
            if topic is None:
                return None
            return {"kind": "topic", "id": topic.id, "name": topic.name}

        return await with_session(operation)

    async def list_sources(_user_id: int, arguments: dict) -> dict:
        async def operation(session):
            values = await SourceRepository(session).list_all()
            return {
                "items": [
                    {
                        "kind": "source",
                        "id": item.id,
                        "name": item.name,
                        "url": item.url,
                        "source_type": item.source_type.value,
                        "enabled": item.enabled,
                        "last_checked_at": item.last_checked_at,
                    }
                    for item in values[: arguments["limit"]]
                ],
                "provenance": {"source": "database"},
            }

        return await with_session(operation)

    async def get_source(_user_id: int, arguments: dict) -> dict | None:
        async def operation(session):
            source = await SourceRepository(session).get(arguments["source_id"])
            if source is None:
                return None
            return {"kind": "source", "id": source.id, "name": source.name}

        return await with_session(operation)

    async def list_conferences(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            status_value = arguments.get("status")
            status = ConferenceStatus(status_value.upper()) if status_value else None
            values = await _list_accessible_conferences(
                session,
                user_id,
                status=status,
                limit=arguments["limit"],
            )
            return {
                "items": [
                    _conference_mapping(item, status=status) for item in values
                ],
                "provenance": {"source": "database"},
            }

        return await with_session(operation)

    async def get_conference(user_id: int, arguments: dict) -> dict | None:
        async def operation(session):
            repository = ConferenceRepository(session)
            try:
                conference = await repository.require_accessible(
                    user_id, arguments["conference_id"]
                )
            except ValueError:
                return None
            return {
                "kind": "conference",
                "id": conference.id,
                "name": conference.name,
            }

        return await with_session(operation)

    async def find_conferences(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            matches = await _find_accessible_conferences(
                session,
                user_id,
                arguments["query"],
                limit=arguments["limit"],
            )
            return {
                "items": [_conference_mapping(item) for item in matches],
                "provenance": {"source": "database"},
            }

        return await with_session(operation)

    async def conference_context(user_id: int, arguments: dict):
        return await with_session(
            lambda session: KnowledgeGraphService(session).conference_context(
                user_id, arguments["conference_id"]
            )
        )

    async def deadlines(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            today = application_clock.today()
            until = today + timedelta(days=arguments["days"])
            values = await ReminderRepository(session).upcoming(
                user_id,
                today,
                until=until,
            )
            values = [item for item in values if not item.is_test]
            return {
                "items": [
                    {
                        "kind": "deadline",
                        "id": item.deadline_id,
                        "conference": item.conference_name,
                        "date": item.deadline_date,
                        "approximate_year": item.approximate_year,
                        "approximate_month": item.approximate_month,
                        "tentative": item.tentative,
                        "type": item.deadline_type,
                        "provenance": {"source_url": item.source_url},
                    }
                    for item in values[: arguments["limit"]]
                ]
            }

        return await with_session(operation)

    async def search_knowledge(user_id: int, arguments: dict):
        return await with_session(
            lambda session: KnowledgeGraphService(session).search(
                user_id, arguments["query"], arguments["limit"]
            )
        )

    async def related(user_id: int, arguments: dict):
        return await with_session(
            lambda session: KnowledgeGraphService(session).related_conferences(
                user_id, arguments["conference_id"], arguments["limit"]
            )
        )

    async def memories(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            values = await MemoryService(
                session, max_context_chars=settings.agent_max_context_chars
            ).list_memories(user_id)
            return {
                "items": [item.model_dump(mode="json") for item in values[: arguments["limit"]]]
            }

        return await with_session(operation)

    async def active_model(user_id: int, _arguments: dict) -> dict:
        async def operation(session):
            active = await _model_registry(
                session, profiles, settings=settings
            ).get_active(user_id)
            return {
                "profile_id": active.profile_id,
                "model_name": active.model_name,
                "supports_tools": active.supports_tools,
            }

        return await with_session(operation)

    async def search_candidates(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            registry = _model_registry(session, profiles, settings=settings)
            service = ConferenceResearchService(
                session,
                search,
                fetcher,
                _research_ai_service(registry, owner.user_id),
                max_page_bytes=settings.max_page_bytes,
                max_page_chars=settings.max_page_text_chars,
                application_clock=application_clock,
                min_relevance_score=settings.min_relevance_score,
            )
            values = await service.search(
                user_id,
                arguments["query"],
                arguments["limit"],
                page=arguments["page"],
            )
            return {
                "items": [
                    {
                        "kind": "event_candidate",
                        "id": item.id,
                        "title": item.title,
                        "url": item.source_url,
                        "summary": item.summary,
                        "verification_status": item.verification_status.value,
                    }
                    for item in values
                ],
                "new_count": service.last_search_new_count,
                "page": arguments["page"],
                "next_page": arguments["page"] + 1,
                "has_more": bool(values),
                "provenance": {"source": "searxng", "verification": "pending"},
            }

        return await with_session(operation)

    async def inspect_candidate(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            registry = _model_registry(session, profiles, settings=settings)
            service = ConferenceResearchService(
                session,
                search,
                fetcher,
                _research_ai_service(registry, owner.user_id),
                max_page_bytes=settings.max_page_bytes,
                max_page_chars=settings.max_page_text_chars,
                application_clock=application_clock,
                min_relevance_score=settings.min_relevance_score,
            )
            candidate = await service.repository.get_candidate(user_id, arguments["candidate_id"])
            research = await service.inspect(user_id, candidate.id)
            return {
                "kind": "event_candidate",
                "id": candidate.id,
                "title": candidate.title,
                "url": candidate.source_url,
                "research": research.model_dump(mode="json"),
            }

        return await with_session(operation)

    async def list_pending_candidates(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            values = await ConferenceResearchRepository(session).list_candidates(
                user_id,
                CandidateStatus.ACTIVE,
                limit=arguments["limit"],
            )
            items = []
            for candidate in values:
                payload = (
                    candidate.research_payload
                    if isinstance(candidate.research_payload, dict)
                    else {}
                )
                deadlines = payload.get("deadlines", [])
                if not isinstance(deadlines, list):
                    deadlines = []
                items.append(
                    {
                        "kind": "event_candidate",
                        "id": candidate.id,
                        "title": candidate.title,
                        "url": candidate.source_url,
                        "official_url": candidate.official_url,
                        "summary": candidate.summary,
                        "verification_status": candidate.verification_status.value,
                        "start_date": candidate.start_date,
                        "end_date": candidate.end_date,
                        "location": candidate.location,
                        "deadlines": deadlines[:20],
                        "relevance_score": candidate.topic_relevance_score,
                        "relevance_reason": candidate.topic_relevance_reason,
                        "researched_at": candidate.researched_at,
                    }
                )
            return {
                "items": items,
                "count": len(items),
                "provenance": {"source": "database", "status": "ACTIVE"},
            }

        return await with_session(operation)

    async def conference_details(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            conference = await ConferenceRepository(session).require_accessible(
                user_id, arguments["conference_id"]
            )
            if conference.is_test:
                raise ValueError("conference not found")
            deadlines = await ConferenceRepository(session).list_deadlines(conference.id)
            evidence = list(
                await session.scalars(
                    select(ConferenceEvidence)
                    .where(ConferenceEvidence.conference_id == conference.id)
                    .order_by(ConferenceEvidence.observed_at.desc())
                    .limit(20)
                )
            )
            history = list(
                await session.scalars(
                    select(ConferenceChange)
                    .where(ConferenceChange.conference_id == conference.id)
                    .order_by(ConferenceChange.detected_at.desc())
                    .limit(20)
                )
            )
            return {
                **_conference_mapping(conference),
                "deadlines": [
                    {
                        "id": item.id,
                        "type": item.deadline_type.value,
                        "exact_date": item.deadline_date,
                        "approximate_year": item.approximate_year,
                        "approximate_month": item.approximate_month,
                        "tentative": item.tentative,
                        "source_url": item.source_url,
                    }
                    for item in deadlines
                ],
                "evidence": [
                    {
                        "id": item.id,
                        "fact": item.fact_kind,
                        "value": item.fact_value,
                        "source_url": item.source_url,
                        "status": item.verification_status.value,
                    }
                    for item in evidence
                ],
                "history": [
                    {
                        "id": item.id,
                        "field": item.field_name,
                        "old": item.old_value,
                        "new": item.new_value,
                        "source_url": item.source_url,
                    }
                    for item in history
                ],
            }

        return await with_session(operation)

    async def check_tracked(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            registry = _model_registry(session, profiles, settings=settings)
            try:
                receipt = await MonitoringService(
                    session,
                    fetcher,
                    _research_ai_service(registry, owner.user_id),
                    settings.max_page_bytes,
                    settings.max_page_text_chars,
                    search,
                    cache=WebDocumentCacheRepository(session),
                    recipes=ParserRecipeRepository(session),
                    reader=_reader_fallback(settings, fetcher),
                    monitoring_interval_hours=settings.monitoring_interval_hours,
                ).check_conference(user_id, arguments["conference_id"])
            except MonitoringCheckError as exc:
                return _monitoring_failure_mapping(arguments["conference_id"], exc)
            return _receipt_mapping(receipt)

        return await with_session(operation)

    async def start_conference_research(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            engine, progress = _durable_conference_workflow(
                session,
                user_id,
                settings,
                profiles,
                search,
                fetcher,
                application_clock,
            )
            run = await engine.start(
                user_id,
                arguments["request_text"],
                await WorkflowPlanner().plan(user_id, arguments["request_text"]),
                progress,
                budget=WorkflowBudget(max_tasks=20),
            )
            return run.model_dump(mode="json")

        return await with_session(operation)

    async def get_agent_run(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            run = await AgentWorkflowRepository(session).get_run(
                user_id, arguments["run_id"]
            )
            return AgentRunView.model_validate(run).model_dump(mode="json")

        return await with_session(operation)

    async def list_agent_run_candidates(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            repository = AgentWorkflowRepository(session)
            await repository.get_run(user_id, arguments["run_id"])
            artifacts = [
                item
                for item in await repository.list_artifacts(arguments["run_id"])
                if item.artifact_kind == "conference_candidate"
            ][: arguments["limit"]]
            return {
                "items": [
                    {
                        "artifact_id": item.id,
                        "validation_state": item.validation_state.value,
                        **item.payload,
                    }
                    for item in artifacts
                ]
            }

        return await with_session(operation)

    async def cancel_agent_run(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            repository = AgentWorkflowRepository(session)
            run = await WorkflowEngine(repository, {}).cancel(
                user_id, arguments["run_id"]
            )
            return run.model_dump(mode="json")

        return await with_session(operation)

    async def persist_agent_candidate(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            repository = AgentWorkflowRepository(session)
            await repository.get_run(user_id, arguments["run_id"])
            artifact = next(
                (
                    item
                    for item in await repository.list_artifacts(arguments["run_id"])
                    if item.id == arguments["artifact_id"]
                    and item.artifact_kind == "conference_candidate"
                    and item.validation_state
                    in {
                        ArtifactValidationState.ACCEPTED,
                        ArtifactValidationState.TENTATIVE,
                    }
                ),
                None,
            )
            candidate_id = artifact.payload.get("candidate_id") if artifact else None
            if isinstance(candidate_id, int):
                return {"candidate_id": candidate_id}

            research = artifact.payload.get("research") if artifact else None
            if isinstance(research, dict):
                title = research.get("name")
                source_url = research.get("source_url")
                if isinstance(title, str) and isinstance(source_url, str):
                    return {"title": title, "source_url": source_url}
            raise ValueError("candidate artifact not found")

        return await with_session(operation)

    async def list_agent_progress(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            repository = AgentWorkflowRepository(session)
            await repository.get_run(user_id, arguments["run_id"])
            events = (
                await repository.list_progress_after(
                    arguments["run_id"], arguments["after_sequence"]
                )
            )[: arguments["limit"]]
            return {
                "items": [
                    {
                        "sequence": item.sequence,
                        "event_type": item.event_type.value,
                        "message": item.message,
                        "safe_data": item.safe_data,
                    }
                    for item in events
                ]
            }

        return await with_session(operation)

    async def get_discovery_search_log(user_id: int, arguments: dict) -> dict:
        """Evidence-based search history: SearXNG results are NOT fetched pages."""
        from conference_planner.db.models import DiscoveryJob, DiscoverySearchAudit

        async def operation(session):
            job_id = arguments.get("job_id")
            if job_id is None:
                job = await session.scalar(select(DiscoveryJob).where(
                    DiscoveryJob.user_id == user_id
                ).order_by(DiscoveryJob.id.desc()).limit(1))
            else:
                job = await session.scalar(select(DiscoveryJob).where(
                    DiscoveryJob.user_id == user_id,
                    DiscoveryJob.id == job_id,
                ))
            if job is None:
                return {"status": "NO_JOB", "items": []}
            rows = list(await session.scalars(select(DiscoverySearchAudit).where(
                DiscoverySearchAudit.job_id == job.id,
            ).order_by(DiscoverySearchAudit.id).limit(arguments.get("limit", 20))))
            return {
                "job_id": job.id,
                "job_status": job.status,
                "source": "SearXNG search log; not verified website visits",
                "search_requests": sum(row.query != "[candidate page]" for row in rows),
                "search_result_count": sum(row.result_count for row in rows if row.query != "[candidate page]"),
                "investigated_pages": sum(row.status in ("RESEARCHED", "CATALOG_EXPANDED") for row in rows),
                "items": [{
                    "query": row.query, "page": row.page,
                    "result_count": row.result_count,
                    "retained_count": row.retained_count,
                    "ignored_count": row.ignored_count,
                    "status": row.status,
                    "unresponsive_engines": row.diagnostics,
                    "urls": row.urls[:25],
                } for row in rows],
            }

        return await with_session(operation)

    def make_research_service(session):
        registry = _model_registry(session, profiles, settings=settings)
        return ConferenceResearchService(
            session, search, fetcher,
            _research_ai_service(registry, owner.user_id),
            max_page_bytes=settings.max_page_bytes,
            max_page_chars=settings.max_page_text_chars,
            application_clock=application_clock,
            min_relevance_score=settings.min_relevance_score,
        )

    async def verify_conference_page(user_id: int, arguments: dict) -> dict:
        from conference_planner.conferences.research import CatalogExpansion
        from conference_planner.schemas.common import CandidateStatus
        async def operation(session):
            service = make_research_service(session)
            candidate = await service.repository.get_candidate(user_id, arguments["candidate_id"])
            if candidate.status != CandidateStatus.ACTIVE:
                return {"candidate_id": candidate.id, "status": "NOT_ACTIVE"}
            try:
                result = await service.inspect(user_id, candidate.id)
            except Exception as exc:
                # No verification claims when the page/LLM cannot be reached.
                return {"candidate_id": candidate.id, "url": candidate.source_url,
                        "status": "NOT_VERIFIED", "error_type": type(exc).__name__}
            if isinstance(result, CatalogExpansion):
                return {"candidate_id": candidate.id, "url": candidate.source_url,
                        "status": "CATALOG_EXPANDED", "child_ids": list(result.created_candidate_ids)}
            return {"candidate_id": candidate.id, "url": candidate.source_url,
                    "status": "RESEARCHED", "research": result.model_dump(mode="json")}
        return await with_session(operation)

    async def expand_conference_catalog(user_id: int, arguments: dict) -> dict:
        result = await verify_conference_page(user_id, arguments)
        if result.get("status") == "RESEARCHED":
            return {"status": "SINGLE_EVENT", "candidate_id": arguments["candidate_id"],
                    "research": result["research"]}
        return result

    async def search_official_sources(user_id: int, arguments: dict) -> dict:
        from conference_planner.conferences.agent_tools import official_site_queries
        from conference_planner.db.models import TrustedSource
        from conference_planner.schemas.common import SourceType
        async def operation(session):
            sources = list(await session.scalars(select(TrustedSource).where(
                TrustedSource.enabled.is_(True), TrustedSource.source_type == SourceType.DOMAIN
            )))
            queries = official_site_queries(arguments["topic"], arguments["year"],
                                             [x.url for x in sources], arguments["limit"])
            if not queries:
                return {"status": "NO_ENABLED_OFFICIAL_DOMAINS", "queries": [], "items": []}
            service = make_research_service(session)
            output, errors = [], []
            for query in queries:
                try:
                    candidates = await service.search(user_id, query, limit=10)
                except Exception as exc:
                    errors.append({"query": query, "error_type": type(exc).__name__})
                    continue
                output.append({"query": query, "candidates": [
                    {"id": candidate.id, "title": candidate.title,
                     "url": candidate.source_url, "status": "UNVERIFIED"}
                    for candidate in candidates[:10]]})
            return {"status": "SEARCHED" if output else "SEARCH_UNAVAILABLE",
                    "queries": queries, "results": output, "errors": errors,
                    "note": "SearXNG result is not proof of official conference status."}
        return await with_session(operation)

    async def verify_deadlines(user_id: int, arguments: dict) -> dict:
        from conference_planner.conferences.agent_tools import summarize_deadlines
        async def operation(session):
            service = make_research_service(session)
            candidate = await service.repository.get_candidate(user_id, arguments["candidate_id"])
            snapshot = await service.repository.load_candidate_snapshot(user_id, candidate.id)
            if snapshot is None:
                return {"candidate_id": candidate.id, "status": "NOT_RESEARCHED",
                        "next_step": "verify_conference_page"}
            return {"candidate_id": candidate.id, **summarize_deadlines(snapshot)}
        return await with_session(operation)

    async def deduplicate_events(user_id: int, arguments: dict) -> dict:
        from conference_planner.conferences.agent_tools import compare_events
        from conference_planner.conferences.deduplication import CandidateIdentity
        from conference_planner.db.models import EventCandidate
        async def operation(session):
            service = make_research_service(session)
            candidate = await service.repository.get_candidate(user_id, arguments["candidate_id"])
            accessible = await _accessible_conference_ids(session, user_id)
            conferences = list(await session.scalars(select(Conference).where(
                Conference.id.in_(accessible), Conference.is_test.is_(False)
            ).limit(arguments["limit"]))) if accessible else []
            other_candidates = list(await session.scalars(select(EventCandidate).where(
                EventCandidate.user_id == user_id, EventCandidate.id != candidate.id
            ).limit(arguments["limit"])))
            identity = CandidateIdentity(candidate.id, candidate.title, candidate.start_date,
                                         None, candidate.official_url or candidate.source_url)
            persisted = [CandidateIdentity(x.id, x.name, x.start_date, x.acronym, x.website)
                         for x in conferences]
            pending = [CandidateIdentity(x.id, x.title, x.start_date, None,
                                         x.official_url or x.source_url) for x in other_candidates]
            return {"candidate_id": candidate.id,
                    "persisted": compare_events(identity, persisted),
                    "other_candidates": compare_events(identity, pending),
                    "action": "REVIEW_ONLY", "automatic_merge": False}
        return await with_session(operation)

    async def search_strategy_refine(user_id: int, arguments: dict) -> dict:
        from conference_planner.conferences.agent_tools import refine_queries
        return refine_queries(arguments["topic"], arguments["year"],
                              arguments.get("previous_queries", []))

    async def list_candidate_decisions(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            status = CandidateStatus(arguments["status"])
            if status not in {CandidateStatus.SKIPPED, CandidateStatus.REJECTED}:
                raise ValueError("only hidden candidate decisions can be listed")
            values = await ConferenceResearchRepository(session).list_candidates_by_statuses(
                user_id,
                [status],
                limit=arguments["limit"],
            )
            return {
                "items": [
                    {
                        "kind": "event_candidate",
                        "id": item.id,
                        "title": item.title,
                        "url": item.source_url,
                        "status": item.status.value,
                        "reason": (
                            item.decision_reason.value if item.decision_reason else None
                        ),
                        "decided_at": item.decision_at,
                        "next_review_at": item.next_review_at,
                    }
                    for item in values
                ],
                "provenance": {"source": "database"},
            }

        return await with_session(operation)

    return {
        "list_topics": list_topics,
        "get_topic": get_topic,
        "list_sources": list_sources,
        "get_source": get_source,
        "list_conferences": list_conferences,
        "get_conference": get_conference,
        "find_conferences": find_conferences,
        "get_conference_context": conference_context,
        "list_upcoming_deadlines": deadlines,
        "search_knowledge": search_knowledge,
        "find_related_conferences": related,
        "list_memories": memories,
        "get_active_model": active_model,
        "search_conference_candidates": search_candidates,
        "list_pending_candidates": list_pending_candidates,
        "get_candidate": inspect_candidate,
        "list_conference_details": conference_details,
        "check_tracked_conference": check_tracked,
        "start_conference_research": start_conference_research,
        "get_agent_run": get_agent_run,
        "list_agent_run_candidates": list_agent_run_candidates,
        "cancel_agent_run": cancel_agent_run,
        "propose_persist_agent_candidate": persist_agent_candidate,
        "list_agent_progress": list_agent_progress,
        "get_discovery_search_log": get_discovery_search_log,
        "verify_conference_page": verify_conference_page,
        "search_official_sources": search_official_sources,
        "expand_conference_catalog": expand_conference_catalog,
        "verify_deadlines": verify_deadlines,
        "deduplicate_events": deduplicate_events,
        "search_strategy_refine": search_strategy_refine,
        "list_candidate_decisions": list_candidate_decisions,
    }


async def _accessible_conference_ids(session: AsyncSession, user_id: int) -> set[int]:
    by_topic = (
        select(ConferenceTopic.conference_id)
        .join(Conference, Conference.id == ConferenceTopic.conference_id)
        .join(Topic, Topic.id == ConferenceTopic.topic_id)
        .where(
            Topic.user_id == user_id,
            Conference.is_test.is_(False),
            Conference.is_catalog_source.is_(False),
        )
    )
    by_subscription = (
        select(Subscription.conference_id)
        .join(Conference, Conference.id == Subscription.conference_id)
        .where(
            Subscription.user_id == user_id,
            Conference.is_test.is_(False),
            Conference.is_catalog_source.is_(False),
        )
    )
    return set(await session.scalars(union(by_topic, by_subscription)))


async def _list_accessible_conferences(
    session: AsyncSession,
    user_id: int,
    *,
    status: ConferenceStatus | None,
    limit: int,
) -> list[Conference]:
    repository = ConferenceRepository(session)
    if status is not None:
        return [
            item
            for item in await repository.list_accessible_by_status(user_id, status)
            if not item.is_test
        ][:limit]
    ids = await _accessible_conference_ids(session, user_id)
    if not ids:
        return []
    values = list(
        await session.scalars(
            select(Conference)
            .where(
                Conference.id.in_(ids),
                Conference.is_catalog_source.is_(False),
            )
            .order_by(Conference.start_date, Conference.id)
        )
    )
    return values[:limit]


async def _find_accessible_conferences(
    session: AsyncSession,
    user_id: int,
    query: str,
    *,
    limit: int,
) -> list[Conference]:
    ids = await _accessible_conference_ids(session, user_id)
    if not ids:
        return []
    needle = normalize_name(query)
    values = list(
        await session.scalars(
            select(Conference).where(
                Conference.id.in_(ids),
                Conference.is_catalog_source.is_(False),
            )
        )
    )
    matches = [
        item
        for item in values
        if needle in item.normalized_name
        or item.normalized_name in needle
        or (item.acronym and needle in normalize_name(item.acronym))
    ]
    matches.sort(
        key=lambda item: (
            item.normalized_name != needle,
            len(item.normalized_name),
            item.id,
        )
    )
    return matches[:limit]


def _conference_mapping(
    conference: Conference,
    *,
    status: ConferenceStatus | None = None,
) -> dict[str, Any]:
    return {
        "kind": "conference",
        "id": conference.id,
        "name": conference.name,
        "start_date": conference.start_date,
        "location": conference.location,
        "status": (status or conference.status).value,
        "provenance": {"source_url": conference.source_url},
    }


def _receipt_mapping(receipt) -> dict[str, Any]:
    if isinstance(receipt, CatalogConversion):
        return {
            "kind": "catalog_conversion",
            "committed": receipt.committed,
            "conference_id": receipt.conference_id,
            "conference_name": receipt.conference_name,
            "source_url": receipt.source_url,
            "candidate_ids": list(receipt.candidate_ids),
            "summary": receipt.telegram_summary(),
        }
    return {
        "kind": "persistence_receipt",
        "committed": receipt.committed,
        "conference_id": receipt.conference_id,
        "conference_name": receipt.conference_name,
        "created": receipt.created,
        "tracked": receipt.tracked,
        "created_fields": list(receipt.created_fields),
        "updated_fields": list(receipt.updated_fields),
        "deadline_ids": [item.id for item in receipt.deadlines],
        "evidence_ids": list(receipt.evidence_ids),
        "change_ids": list(receipt.change_ids),
        "summary": receipt.telegram_summary(),
    }


def _monitoring_failure_mapping(
    conference_id: int, error: MonitoringCheckError
) -> dict[str, Any]:
    code = str(error)
    if code == "NO_TEMPORAL_DATA":
        summary = (
            "Страницы проверены, но подтверждённые даты проведения и дедлайны "
            "не найдены. Данные не изменены. Код: NO_TEMPORAL_DATA."
        )
    elif code in {"NETWORK_ERROR", "TIMEOUT", "FetchError"}:
        summary = (
            "Не удалось открыть источники. Используемого снимка или поисковой "
            f"карточки с датами нет. Данные не изменены. Код: {code}."
        )
    else:
        summary = f"Проверка не завершена; данные не изменены. Код: {code}."
    return {
        "kind": "monitoring_failure",
        "committed": False,
        "conference_id": conference_id,
        "error_code": code,
        "summary": summary,
    }
