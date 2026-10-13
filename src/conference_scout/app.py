import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conference_scout.agent.actions import ConfirmationService
from conference_scout.agent.knowledge import KnowledgeGraphService
from conference_scout.agent.memory import MemoryService
from conference_scout.agent.runtime import AgentRuntime
from conference_scout.ai.active_provider import ActiveLLMProvider
from conference_scout.ai.registry import ModelRegistry, ProviderProfile, build_provider_profiles
from conference_scout.ai.service import AIService
from conference_scout.bot.dependencies import RepositoryMiddleware
from conference_scout.bot.router import build_router
from conference_scout.conferences.discovery import DiscoveryService
from conference_scout.conferences.monitoring import MonitoringService
from conference_scout.config import Settings
from conference_scout.db.migrations import run_migrations
from conference_scout.db.models import Conference, ConferenceTopic, Deadline, Subscription, Topic
from conference_scout.db.repositories.agent_state import AgentStateRepository
from conference_scout.db.repositories.conferences import ConferenceRepository
from conference_scout.db.repositories.memories import MemoryRepository
from conference_scout.db.repositories.reminders import ReminderRepository
from conference_scout.db.repositories.sources import SourceRepository
from conference_scout.db.repositories.topics import TopicRepository
from conference_scout.db.session import create_engine_and_sessionmaker
from conference_scout.mcp.client import StreamableHTTPToolClient
from conference_scout.mcp.lifecycle import MCPServerLifecycle
from conference_scout.mcp.server import build_mcp_app, build_mcp_server
from conference_scout.mcp.tools import MCPToolServices
from conference_scout.owner import OwnerContext, bootstrap_owner
from conference_scout.reminders.service import ReminderCandidate, ReminderService
from conference_scout.scheduler.setup import configure_scheduler
from conference_scout.schemas.agent import (
    AddSourceAction,
    AddTopicAction,
    ArchiveConferenceAction,
    ForgetMemoryAction,
    MemoryProposal,
    RunDiscoveryAction,
    SaveMemoryAction,
    TrackConferenceAction,
)
from conference_scout.schemas.common import ConferenceStatus
from conference_scout.schemas.topics import TopicNormalization
from conference_scout.search.searxng import SearXNGSearchProvider
from conference_scout.web.fetcher import PageFetcher
from conference_scout.web.parser import extract_page_text
from conference_scout.web.url_safety import URLSafetyPolicy

logger = logging.getLogger(__name__)


class CandidateStore:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    def replace(self, results) -> list[dict[str, Any]]:
        self._items.clear()
        values = []
        for result in results:
            candidate_id = f"candidate-{self._next_id}"
            self._next_id += 1
            item = {
                "kind": "event_candidate",
                "id": candidate_id,
                "title": result.title,
                "url": str(result.url),
                "snippet": result.snippet,
                "provenance": {"engine": result.engine, "source_url": str(result.url)},
            }
            self._items[candidate_id] = item
            values.append(item)
        return values

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        return self._items.get(candidate_id)


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
            candidates = CandidateStore()
            read_handlers = _build_read_handlers(
                session_factory,
                owner,
                profiles,
                search=search,
                fetcher=fetcher,
                candidates=candidates,
                settings=settings,
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
) -> None:
    dispatcher = Dispatcher(storage=MemoryStorage())
    router = build_router(owner_telegram_id=owner.telegram_id)

    def services(session: AsyncSession) -> dict[str, Any]:
        registry = ModelRegistry(
            AgentStateRepository(session),
            profiles,
            capability_timeout=min(20, settings.agent_timeout_seconds),
        )
        memory = MemoryService(session, max_context_chars=settings.agent_max_context_chars)
        confirmation = ConfirmationService(
            session,
            executors=_build_action_executors(
                session, owner.user_id, settings, registry, search, fetcher
            ),
        )
        return {
            "model_registry": registry,
            "memory_service": memory,
            "confirmation_service": confirmation,
            "agent_runtime": AgentRuntime(
                registry,
                memory,
                tool_client,
                max_steps=settings.agent_max_steps,
                max_tool_calls_per_step=settings.agent_max_tool_calls_per_step,
                timeout_seconds=settings.agent_timeout_seconds,
                max_tool_result_chars=settings.agent_max_tool_result_chars,
            ),
            "ai_service": AIService(ActiveLLMProvider(registry, owner.user_id)),
        }

    middleware = RepositoryMiddleware(session_factory, owner.user_id, service_factory=services)
    router.message.middleware(middleware)
    router.callback_query.middleware(middleware)
    dispatcher.include_router(router)

    async def discovery_job() -> None:
        async with session_factory() as session:
            registry = ModelRegistry(AgentStateRepository(session), profiles)
            service = _discovery_service(
                session, owner.user_id, settings, registry, search, fetcher
            )
            report = await service.run(
                owner.user_id, years=[date.today().year, date.today().year + 1]
            )
            if report.created_ids:
                await bot.send_message(
                    owner.telegram_id,
                    f"🔎 Найдено новых конференций: {len(report.created_ids)}",
                )

    async def monitoring_job() -> None:
        async with session_factory() as session:
            registry = ModelRegistry(AgentStateRepository(session), profiles)
            ai = AIService(ActiveLLMProvider(registry, owner.user_id))
            report = await MonitoringService(
                session, fetcher, ai, settings.max_page_bytes, settings.max_page_text_chars
            ).run(owner.user_id)
            for change in report.changes:
                await bot.send_message(
                    owner.telegram_id,
                    f"🔄 Изменился дедлайн\n\n{change.conference_name}\n"
                    f"{change.old_date.isoformat()} → {change.new_date.isoformat()}\n"
                    f"{change.source_url}",
                )

    async def reminder_job() -> None:
        async with session_factory() as session:
            repository = ReminderRepository(session)

            async def send(candidate: ReminderCandidate, days: int) -> None:
                await bot.send_message(
                    owner.telegram_id,
                    f"⏰ Через {days} дней: {candidate.conference_name}\n"
                    f"{candidate.deadline_type}: {candidate.deadline_date.isoformat()}",
                )

            await ReminderService(repository, settings.reminder_days).process(
                owner.user_id, date.today(), send
            )

    scheduler = AsyncIOScheduler()
    configure_scheduler(
        scheduler,
        discovery_job=discovery_job,
        monitoring_job=monitoring_job,
        reminder_job=reminder_job,
        discovery_day=settings.discovery_day,
        discovery_hour=settings.discovery_hour,
        monitoring_hour=settings.monitoring_hour,
        timezone=settings.app_timezone,
    )
    scheduler.start()
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)


def _build_action_executors(
    session, owner_id: int, settings, registry, search, fetcher
) -> dict[str, Any]:
    async def add_topic(user_id: int, payload: AddTopicAction) -> str:
        await TopicRepository(session).create(
            user_id,
            TopicNormalization(
                name=payload.name,
                description=payload.description,
                keywords=list(payload.keywords),
            ),
        )
        return "Тема добавлена."

    async def add_source(_user_id: int, payload: AddSourceAction) -> str:
        await SourceRepository(session).create(payload.value, payload.value, payload.source_type)
        return "Источник добавлен."

    async def track(user_id: int, payload: TrackConferenceAction) -> str:
        repository = ConferenceRepository(session)
        await repository.require_accessible(user_id, payload.conference_id)
        await repository.track(user_id, payload.conference_id)
        return "Конференция отслеживается."

    async def archive(user_id: int, payload: ArchiveConferenceAction) -> str:
        repository = ConferenceRepository(session)
        await repository.require_accessible(user_id, payload.conference_id)
        await repository.archive(payload.conference_id)
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
        report = await _discovery_service(
            session, owner_id, settings, registry, search, fetcher
        ).run(user_id, years=list(payload.years))
        return f"Поиск завершён. Новых конференций: {len(report.created_ids)}."

    return {
        "add_topic": add_topic,
        "add_source": add_source,
        "track_conference": track,
        "archive_conference": archive,
        "save_memory": save_memory,
        "forget_memory": forget_memory,
        "run_discovery": run_discovery,
    }


def _discovery_service(session, owner_id, settings, registry, search, fetcher):
    return DiscoveryService(
        session,
        search,
        fetcher,
        AIService(ActiveLLMProvider(registry, owner_id)),
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
    candidates: CandidateStore,
    settings,
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

    async def list_conferences(user_id: int, arguments: dict) -> dict:
        async def operation(session):
            ids = await _accessible_conference_ids(session, user_id)
            statement = select(Conference).where(Conference.id.in_(ids))
            status = arguments.get("status")
            if status:
                statement = statement.where(Conference.status == ConferenceStatus(status.upper()))
            values = list(
                await session.scalars(
                    statement.order_by(Conference.start_date).limit(arguments["limit"])
                )
            )
            return {
                "items": [_conference_mapping(item) for item in values],
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
            ids = await _accessible_conference_ids(session, user_id)
            until = date.today() + timedelta(days=arguments["days"])
            rows = await session.execute(
                select(Deadline, Conference)
                .join(Conference, Conference.id == Deadline.conference_id)
                .where(
                    Deadline.conference_id.in_(ids),
                    Deadline.deadline_date >= date.today(),
                    Deadline.deadline_date <= until,
                )
                .order_by(Deadline.deadline_date)
                .limit(arguments["limit"])
            )
            return {
                "items": [
                    {
                        "kind": "deadline",
                        "id": deadline.id,
                        "conference_id": conference.id,
                        "conference": conference.name,
                        "date": deadline.deadline_date,
                        "type": deadline.deadline_type.value,
                        "provenance": {"source_url": deadline.source_url},
                    }
                    for deadline, conference in rows
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
            active = await ModelRegistry(AgentStateRepository(session), profiles).get_active(
                user_id
            )
            return {
                "profile_id": active.profile_id,
                "model_name": active.model_name,
                "supports_tools": active.supports_tools,
            }

        return await with_session(operation)

    async def search_candidates(_user_id: int, arguments: dict) -> dict:
        results = (await search.search(arguments["query"]))[: arguments["limit"]]
        return {"items": candidates.replace(results)}

    async def inspect_candidate(_user_id: int, arguments: dict) -> dict:
        candidate = candidates.get(arguments["candidate_id"])
        if candidate is None:
            raise ValueError("candidate not found")
        page = await fetcher.fetch(candidate["url"], max_bytes=settings.max_page_bytes)
        text = extract_page_text(page.html, max_chars=min(settings.max_page_text_chars, 8_000))
        return {**candidate, "page_text": text, "observed_at": datetime.now(UTC)}

    return {
        "list_topics": list_topics,
        "list_conferences": list_conferences,
        "get_conference_context": conference_context,
        "list_upcoming_deadlines": deadlines,
        "search_knowledge": search_knowledge,
        "find_related_conferences": related,
        "list_memories": memories,
        "get_active_model": active_model,
        "search_event_candidates": search_candidates,
        "inspect_event_candidate": inspect_candidate,
    }


async def _accessible_conference_ids(session: AsyncSession, user_id: int) -> set[int]:
    by_topic = (
        select(ConferenceTopic.conference_id)
        .join(Topic, Topic.id == ConferenceTopic.topic_id)
        .where(Topic.user_id == user_id)
    )
    by_subscription = select(Subscription.conference_id).where(Subscription.user_id == user_id)
    return set(await session.scalars(union(by_topic, by_subscription)))


def _conference_mapping(conference: Conference) -> dict[str, Any]:
    return {
        "kind": "conference",
        "id": conference.id,
        "name": conference.name,
        "start_date": conference.start_date,
        "location": conference.location,
        "status": conference.status.value,
        "provenance": {"source_url": conference.source_url},
    }
