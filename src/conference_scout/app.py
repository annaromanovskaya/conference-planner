import logging
from datetime import date

import httpx
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from conference_scout.ai.openrouter import OpenRouterLLMProvider
from conference_scout.ai.service import AIService
from conference_scout.bot.dependencies import RepositoryMiddleware
from conference_scout.bot.router import build_router
from conference_scout.conferences.discovery import DiscoveryService
from conference_scout.conferences.monitoring import MonitoringService
from conference_scout.config import Settings
from conference_scout.db.repositories.reminders import ReminderRepository
from conference_scout.db.session import create_engine_and_sessionmaker, initialize_database
from conference_scout.owner import bootstrap_owner
from conference_scout.reminders.service import ReminderCandidate, ReminderService
from conference_scout.scheduler.setup import configure_scheduler
from conference_scout.search.searxng import SearXNGSearchProvider
from conference_scout.web.fetcher import PageFetcher
from conference_scout.web.url_safety import URLSafetyPolicy

logger = logging.getLogger(__name__)


async def run_application(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    engine, session_factory = create_engine_and_sessionmaker(settings.database_url)
    await initialize_database(engine)
    async with session_factory() as session:
        owner = await bootstrap_owner(session, settings.owner_telegram_id)

    bot = Bot(settings.telegram_bot_token.get_secret_value())
    dispatcher = Dispatcher(storage=MemoryStorage())
    router = build_router(owner_telegram_id=owner.telegram_id)
    router.message.middleware(RepositoryMiddleware(session_factory, owner.user_id))
    router.callback_query.middleware(RepositoryMiddleware(session_factory, owner.user_id))
    dispatcher.include_router(router)

    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": "ConferenceScout/0.1"}
    ) as web_client:
        fetcher = PageFetcher(web_client, URLSafetyPolicy())
        search = SearXNGSearchProvider(str(settings.searxng_url), web_client)
        ai = AIService(
            OpenRouterLLMProvider(
                web_client,
                settings.openrouter_api_key.get_secret_value(),
                settings.openrouter_model,
            )
        )

        async def discovery_job() -> None:
            async with session_factory() as session:
                service = DiscoveryService(
                    session,
                    search,
                    fetcher,
                    ai,
                    settings.min_relevance_score,
                    settings.max_page_bytes,
                    settings.max_page_text_chars,
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

        dispatcher["ai_service"] = ai
        dispatcher["discovery_runner"] = discovery_job
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
            await bot.session.close()
            await engine.dispose()
