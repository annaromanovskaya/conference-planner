"""Durable, agent-driven discovery jobs with one live Telegram status message.

The queue is owned by SQLite. A supervisor runs in the polling process with
independent DB sessions; it never borrows an aiogram callback session.
"""
import asyncio
import logging
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update

from conference_planner.db.base import utc_now
from conference_planner.db.models import DiscoveryJob, DiscoverySearchAudit, DiscoveryURLTask
from conference_planner.schemas.common import AgentTaskType, AgentRunStatus, WorkflowKind
from conference_planner.schemas.workflows import WorkflowBudget, WorkflowPlan, WorkflowTaskSpec

logger = logging.getLogger(__name__)
FINISHED = frozenset({"COMPLETED", "EMPTY", "PARTIAL", "FAILED", "CANCELLED"})


def keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Статус", callback_data=f"discovery:status:{job_id}"),
        InlineKeyboardButton(text="📄 Подробности", callback_data=f"discovery:details:{job_id}"),
        InlineKeyboardButton(text="⏹ Отменить", callback_data=f"discovery:cancel:{job_id}"),
    ]])


def summarize_progress(records, tasks):
    """One source of truth for the Telegram card and detailed report."""
    sources = [r for r in records if r.query.startswith("[trusted source]")]
    visits = [r for r in records if r.query == "[candidate page]"]
    searches = [r for r in records if r.query != "[candidate page]" and not r.query.startswith("[trusted source]")]
    return {
        "catalogs": sum(r.status in ("SOURCE_OPENED", "CATALOG_OPENED") for r in sources),
        "researched": sum(r.status == "RESEARCHED" for r in visits),
        "expanded": sum(r.status == "CATALOG_EXPANDED" for r in visits),
        "errors": sum(r.status in ("INSPECTION_FAILED", "SOURCE_ERROR") for r in records),
        "ineligible": sum(r.status in ("NOT_VERIFIABLE", "NOT_CONFERENCE", "EVENT_EXPIRED") for r in visits),
        "pending": sum(t.status == "PENDING" for t in tasks),
        "running": sum(t.status == "RUNNING" for t in tasks),
        "skipped_files": sum(t.status == "SKIPPED" and t.error_code == "NON_HTML_DOCUMENT" for t in tasks),
        "queries": len(searches),
        "search_hits": sum(r.result_count for r in searches),
    }


def progress_lines(metrics):
    return [
        f"Открыто страниц источников: {metrics['catalogs']}",
        f"Успешно исследовано страниц кандидатов: {metrics['researched']}",
        f"Раскрыто каталогов при проверке: {metrics['expanded']}",
        f"Отклонено по содержимому / срокам: {metrics['ineligible']}",
        f"Технических ошибок: {metrics['errors']}",
        f"Очередь: {metrics['pending']} ожидают, {metrics['running']} выполняются",
        f"Пропущено файлов: {metrics['skipped_files']}",
    ]


def render(job: DiscoveryJob, metrics=None) -> str:
    states = {
        "QUEUED": "⏳ Поиск конференций в очереди",
        "RUNNING": "🔎 Поиск конференций выполняется",
        "CANCEL_REQUESTED": "⏹ Останавливаю поиск конференций",
        "COMPLETED": "✅ Поиск конференций завершён",
        "EMPTY": "🔎 Поиск завершён: подходящие конференции не найдены",
        "PARTIAL": "⚠️ Поиск завершён частично",
        "FAILED": "❌ Ошибка поиска конференций",
        "CANCELLED": "⏹ Поиск конференций остановлен",
    }
    stages = {"QUEUED": "Ожидание", "SEARCH": "Агент: поиск и анализ сайтов",
              "VERIFY": "Проверка найденных страниц", "DONE": "Завершено"}
    lines = [states.get(job.status, job.status),
             f"Годы: {', '.join(map(str, job.years))}",
             f"Этап: {stages.get(job.stage, job.stage)}", ""]
    if metrics is not None:
        lines.extend(progress_lines(metrics))
    else:
        lines.extend([f"Исследовано сайтов: {job.processed}",
                      f"Сохранено ссылок-кандидатов: {job.found}",
                      f"Отклонено: {job.rejected}", f"Ошибки: {job.errors}"])
    if job.error_code and job.status not in ("QUEUED", "RUNNING"):
        lines.append(f"Причина: {job.error_code[:120]}")
    return "\n".join(lines)


class DiscoveryProgressController:
    def __init__(self, bot, session_factory, owner_id, telegram_id,
                 settings, profiles, search, fetcher, application_clock):
        self.bot, self.sessions = bot, session_factory
        self.owner_id, self.telegram_id = owner_id, telegram_id
        self.settings, self.profiles = settings, profiles
        self.search, self.fetcher, self.clock = search, fetcher, application_clock
        self.tasks: list[asyncio.Task] = []
        self._last: dict[int, str] = {}
        self._report_last: dict[int, str] = {}
        self._report_locks: dict[int, asyncio.Lock] = {}

    async def enqueue_query(self, user_id: int, query: str, message):
        """Idempotent entry point for free-text discovery requests."""
        import re
        from datetime import datetime, timezone
        years = sorted({int(x) for x in re.findall(r"\b20\d{2}\b", query)})
        if not years:
            years = [datetime.now(timezone.utc).year]
        async with self.sessions() as session:
            active = await session.scalar(select(DiscoveryJob).where(
                DiscoveryJob.user_id == user_id,
                DiscoveryJob.status.in_(["QUEUED", "RUNNING", "CANCEL_REQUESTED"]),
            ).order_by(DiscoveryJob.id.desc()).limit(1))
            if active is None:
                active = DiscoveryJob(
                    user_id=user_id, years=years, query=query,
                    status="QUEUED", stage="QUEUED",
                    created_at=utc_now(), updated_at=utc_now(),
                )
                session.add(active)
                await session.commit()
                await session.refresh(active)
            job_id = active.id
            old_message_id = active.message_id
            old_report_id = active.report_message_id
            metrics = await self._progress(session, job_id)
            body = render(active, metrics)
        # Reusing the same background job must not force the user to scroll to
        # an old Telegram message. Create a fresh live card for this request.
        # Do not hold a DB transaction while talking to Telegram.
        sent = await message.answer(body, reply_markup=keyboard(job_id))
        async with self.sessions() as session:
            job = await session.get(DiscoveryJob, job_id)
            if job is not None:
                job.chat_id = sent.chat.id
                job.message_id = sent.message_id
                job.report_message_id = None  # New report belongs beneath new card.
                job.notified = False
                await session.commit()
        self._last.pop(job_id, None)
        self._report_last.pop(job_id, None)
        # Old cards must no longer offer misleading controls or look live.
        if old_message_id is not None and old_message_id != sent.message_id:
            try:
                await self.bot.edit_message_reply_markup(
                    chat_id=sent.chat.id, message_id=old_message_id, reply_markup=None
                )
            except (TelegramBadRequest, TelegramRetryAfter):
                pass
        if old_report_id is not None:
            await self.update_report(job_id, create=True)
        return job_id

    def enqueue_callback(self, session):
        async def enqueue(user_id: int, years: list[int]):
            active = await session.scalar(select(DiscoveryJob).where(
                DiscoveryJob.user_id == user_id,
                DiscoveryJob.status.in_(["QUEUED", "RUNNING", "CANCEL_REQUESTED"]),
            ).order_by(DiscoveryJob.id.desc()).limit(1))
            if active:
                return active
            job = DiscoveryJob(user_id=user_id, years=years, status="QUEUED",
                               stage="QUEUED", created_at=utc_now(), updated_at=utc_now())
            session.add(job)
            await session.flush()
            return job
        return enqueue

    async def attach_message(self, job_id: int, message):
        async with self.sessions() as session:
            job = await session.get(DiscoveryJob, job_id)
            if job is None:
                return
            if job.message_id is not None:
                return
            sent = await message.answer(render(job), reply_markup=keyboard(job.id))
            job.chat_id, job.message_id = sent.chat.id, sent.message_id
            await session.commit()

    def router(self):
        router = Router(name="discovery-progress")

        @router.callback_query(F.data.startswith("discovery:status:"))
        async def status(callback: CallbackQuery):
            job = await self._owned_job(callback)
            if job:
                async with self.sessions() as session:
                    metrics = await self._progress(session, job.id)
                await callback.answer(
                    f"{job.status}: {metrics['catalogs']} каталогов, "
                    f"{metrics['researched']} проверено, {metrics['pending']} в очереди, "
                    f"{metrics['errors']} ошибок", show_alert=True)

        @router.callback_query(F.data.startswith("discovery:details:"))
        async def details(callback: CallbackQuery):
            job = await self._owned_job(callback)
            if job is None:
                return
            await callback.answer()
            await self.update_report(job.id, create=True)

        @router.callback_query(F.data.startswith("discovery:cancel:"))
        async def cancel(callback: CallbackQuery):
            job = await self._owned_job(callback)
            if job is None:
                return
            async with self.sessions() as session:
                result = await session.execute(update(DiscoveryJob).where(
                    DiscoveryJob.id == job.id, DiscoveryJob.user_id == self.owner_id,
                    DiscoveryJob.status.in_(["QUEUED", "RUNNING"]),
                ).values(status="CANCEL_REQUESTED", updated_at=utc_now()))
                await session.commit()
            await callback.answer("Остановка запрошена" if result.rowcount else "Задание уже остановлено")
        return router

    async def update_report(self, job_id: int, *, create: bool = False) -> None:
        """Keep a single report message per job, directly under its status card.

        The report ID survives restarts. Subsequent clicks and notifier refreshes
        edit that same message instead of posting duplicate reports.
        """
        lock = self._report_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            async with self.sessions() as session:
                job = await session.get(DiscoveryJob, job_id)
                if job is None or job.chat_id is None:
                    return
                chat_id, report_id = job.chat_id, job.report_message_id
            if report_id is None and not create:
                return
            body = await self.describe_search(job_id)
            if report_id is not None:
                if self._report_last.get(job_id) == body and not create:
                    return
                try:
                    await self.bot.edit_message_text(
                        chat_id=chat_id, message_id=report_id, text=body,
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        logger.warning("Discovery report edit failed for job %s: %s", job_id, exc)
                        return
                except TelegramRetryAfter:
                    return
                self._report_last[job_id] = body
                return
            sent = await self.bot.send_message(chat_id=chat_id, text=body)
            async with self.sessions() as session:
                await session.execute(update(DiscoveryJob).where(
                    DiscoveryJob.id == job_id,
                    DiscoveryJob.report_message_id.is_(None),
                ).values(report_message_id=sent.message_id))
                await session.commit()
            self._report_last[job_id] = body

    async def describe_search(self, job_id: int) -> str:
        async with self.sessions() as session:
            records = list(await session.scalars(select(DiscoverySearchAudit).where(
                DiscoverySearchAudit.job_id == job_id
            ).order_by(DiscoverySearchAudit.id)))
        if not records:
            return "Журнал поисковых запросов пока пуст. Ни одна страница не подтверждена."
        searches = [row for row in records if row.query != "[candidate page]" and not row.query.startswith("[trusted source]")]
        async with self.sessions() as session:
            tasks = list(await session.scalars(select(DiscoveryURLTask).where(
                DiscoveryURLTask.job_id == job_id)))
        metrics = summarize_progress(records, tasks)
        lines = [f"📄 Отчёт о поиске №{job_id}",
                 f"Запросов к SearXNG: {metrics['queries']}",
                 f"Ссылок в выдаче (с повторами): {metrics['search_hits']}",
                 *progress_lines(metrics), ""]
        for row in records[-12:]:
            if row.query == "[candidate page]":
                lines.append(f"• Проверка сайта: {row.status}")
            else:
                lines.append(f"• {row.query[:150]} [стр. {row.page}]: {row.result_count} результатов, "
                             f"{row.retained_count} предварительных кандидатов")
            for hit in (row.urls or [])[:5]:
                lines.append(f"  {hit.get('decision')}: {hit.get('url')}")
            if row.diagnostics:
                label = "Диагностика" if row.query == "[candidate page]" else "Ошибки / диагностика"
                lines.append(f"  {label}: {', '.join(row.diagnostics)[:240]}")
        return "\n".join(lines)[:3900]

    async def _progress(self, session, job_id):
        audits = list(await session.scalars(select(DiscoverySearchAudit).where(
            DiscoverySearchAudit.job_id == job_id)))
        tasks = list(await session.scalars(select(DiscoveryURLTask).where(
            DiscoveryURLTask.job_id == job_id)))
        return summarize_progress(audits, tasks)

    async def latest_search_report(self, user_id: int) -> str:
        async with self.sessions() as session:
            job = await session.scalar(select(DiscoveryJob).where(
                DiscoveryJob.user_id == user_id
            ).order_by(DiscoveryJob.id.desc()).limit(1))
        return await self.describe_search(job.id) if job else "Поисковых заданий ещё нет."

    async def _owned_job(self, callback):
        try:
            job_id = int(callback.data.rsplit(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("Некорректный идентификатор", show_alert=True)
            return None
        if callback.from_user.id != self.telegram_id:
            await callback.answer("Нет доступа", show_alert=True)
            return None
        async with self.sessions() as session:
            job = await session.get(DiscoveryJob, job_id)
            if job is None or job.user_id != self.owner_id:
                await callback.answer("Задание не найдено", show_alert=True)
                return None
            return job

    async def start(self):
        # A previous worker may have died; the queue is replayed on startup.
        async with self.sessions() as session:
            await session.execute(update(DiscoveryJob).where(
                DiscoveryJob.status == "RUNNING"
            ).values(status="QUEUED", stage="QUEUED", updated_at=utc_now()))
            # No worker from the previous process is alive: reclaim in-flight
            # URL tasks immediately rather than waiting for stale leases.
            from conference_planner.db.models import DiscoveryURLTask
            await session.execute(update(DiscoveryURLTask).where(
                DiscoveryURLTask.status == "RUNNING",
            ).values(status="PENDING", lease_until=None, next_retry_at=None))
            await session.commit()
        self.tasks = [asyncio.create_task(self._worker(), name="discovery-worker"),
                      asyncio.create_task(self._notifier(), name="discovery-notifier")]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def _worker(self):
        while True:
            try:
                async with self.sessions() as session:
                    job = await session.scalar(select(DiscoveryJob).where(
                        DiscoveryJob.status.in_(["QUEUED", "CANCEL_REQUESTED"])
                    ).order_by(DiscoveryJob.id).limit(1))
                    job_id = job.id if job is not None else None
                    if job_id is not None and job.status == "CANCEL_REQUESTED":
                        job.status = "CANCELLED"
                        await session.commit()
                        job_id = None
                    elif job_id is not None:
                        years = list(job.years)
                        query = job.query
                        job.status, job.stage = "RUNNING", "SEARCH"
                        job.processed = job.found = job.rejected = job.errors = 0
                        job.total = None
                        job.notified = False
                        await session.commit()
                if job_id is None:
                    await asyncio.sleep(2)
                    continue
                await self._run_agent(job_id, years, query)
                # Avoid a tight retry cycle when all remaining URL tasks are
                # cooling down. Other Telegram operations stay responsive.
                async with self.sessions() as session:
                    from conference_planner.db.models import DiscoveryURLTask
                    from sqlalchemy import func
                    delay_until = await session.scalar(select(func.min(DiscoveryURLTask.next_retry_at)).where(
                        DiscoveryURLTask.job_id == job_id,
                        DiscoveryURLTask.status == "PENDING",
                        DiscoveryURLTask.next_retry_at.is_not(None),
                    ))
                    pending_ready = await session.scalar(select(func.count()).select_from(DiscoveryURLTask).where(
                        DiscoveryURLTask.job_id == job_id,
                        DiscoveryURLTask.status == "PENDING",
                        DiscoveryURLTask.next_retry_at.is_(None),
                    ))
                if delay_until is not None and not pending_ready:
                    wait = (delay_until - utc_now()).total_seconds()
                    await asyncio.sleep(min(30, max(2, wait)))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("discovery worker failed")
                await asyncio.sleep(3)

    async def _run_agent(self, job_id: int, years: list[int], query: str | None = None):

        # Use the existing agent workflow engine; every run is recorded in agent_runs/tasks.
        from conference_planner.app import _durable_conference_workflow

        class StopRequested(Exception):
            pass

        async def on_progress(report, _url):
            async with self.sessions() as session:
                job = await session.get(DiscoveryJob, job_id)
                if job is None or job.status == "CANCEL_REQUESTED":
                    raise StopRequested()
                job.processed += getattr(report, "pages_checked", 0)
                job.found = len(report.created_ids)
                job.rejected = len(report.rejected_urls)
                job.errors = len(report.errors)
                job.updated_at = utc_now()
                await session.commit()

        class RunProgress:
            async def emit(_self, run_id, event):
                async with self.sessions() as session:
                    job = await session.get(DiscoveryJob, job_id)
                    if job:
                        job.agent_run_id = run_id
                        await session.commit()
                # Persist the event through the existing agent event repository.
                from conference_planner.workflows.progress import WorkflowProgress
                from conference_planner.db.repositories.agent_workflows import AgentWorkflowRepository
                async with self.sessions() as session:
                    await WorkflowProgress(AgentWorkflowRepository(session)).emit(run_id, event)
                    await session.commit()

        try:
            async with self.sessions() as session:
                engine, _ = _durable_conference_workflow(
                    session, self.owner_id, self.settings, self.profiles,
                    self.search, self.fetcher, self.clock,
                    on_discovery_progress=on_progress,
                    discovery_job_id=job_id,
                )
                plan = WorkflowPlan(tasks=(WorkflowTaskSpec(
                    task_id="search", task_type=AgentTaskType.SEARCH_CANDIDATES,
                    input_data={"query": query} if query else {"years": years},
                ),))
                result = await engine.start(
                    self.owner_id, query or f"Поиск конференций на {', '.join(map(str, years))}",
                    plan, RunProgress(), budget=WorkflowBudget(max_tasks=1, timeout_seconds=1800,
                                          max_pages=500), workflow_kind=WorkflowKind.CONFERENCE_RESEARCH,
                )
                await session.commit()
            async with self.sessions() as session:
                job = await session.get(DiscoveryJob, job_id)
                if job:
                    # A successfully executed tool can still report an empty or
                    # unavailable search: do not label that as verified success.
                    from conference_planner.db.models import AgentArtifact
                    artifacts = list(await session.scalars(select(AgentArtifact).where(
                        AgentArtifact.run_id == result.id
                    )))
                    candidate_payload = next((a.payload for a in artifacts
                                              if a.artifact_kind == "candidate_list"), {})
                    search_code = candidate_payload.get("error_code")
                    pending_tasks = int(candidate_payload.get("pending_url_tasks") or 0)
                    job.status = "CANCELLED" if job.status == "CANCEL_REQUESTED" else (
                        "PARTIAL" if result.status != AgentRunStatus.COMPLETED else
                        "EMPTY" if search_code in ("SEARCH_EMPTY", "NO_RELEVANT_RESULTS") else
                        "PARTIAL" if search_code else "COMPLETED")
                    job.error_code = search_code
                    if pending_tasks and result.status == AgentRunStatus.COMPLETED and job.status != "CANCELLED":
                        job.status = "QUEUED"
                        job.stage = "SEARCH"
                    else:
                        job.stage = "DONE"
                    job.agent_run_id = result.id
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except StopRequested:
            await self._finish(job_id, "CANCELLED")
        except Exception:
            logger.exception("agent discovery failed, job_id=%s", job_id)
            await self._finish(job_id, "FAILED", "AGENT_DISCOVERY_FAILED")

    async def _finish(self, job_id, status, error_code=None):
        async with self.sessions() as session:
            job = await session.get(DiscoveryJob, job_id)
            if job:
                job.status, job.stage, job.error_code = status, "DONE", error_code
                await session.commit()

    async def _notifier(self):
        while True:
            try:
                # Never keep a database transaction open across Telegram API calls.
                async with self.sessions() as session:
                    rows = list(await session.scalars(select(DiscoveryJob).where(
                        DiscoveryJob.message_id.is_not(None),
                        DiscoveryJob.chat_id.is_not(None),
                    ).order_by(DiscoveryJob.id.desc()).limit(30)))
                    snapshots = []
                    for job in rows:
                        metrics = await self._progress(session, job.id)
                        snapshots.append((
                            job.id, job.chat_id, job.message_id, job.status,
                            job.notified, render(job, metrics),
                        ))
                for job_id, chat_id, message_id, status, notified, body in snapshots:
                    try:
                        await self.update_report(job_id)
                    except Exception:
                        logger.exception("Discovery report refresh failed for job %s", job_id)
                    if status in FINISHED and notified:
                        continue
                    if self._last.get(job_id) == (body, status):
                        continue
                    try:
                        await self.bot.edit_message_text(
                            text=body, chat_id=chat_id, message_id=message_id,
                            reply_markup=None if status in FINISHED else keyboard(job_id),
                        )
                    except TelegramRetryAfter:
                        continue
                    except TelegramBadRequest as exc:
                        if "message is not modified" not in str(exc).lower():
                            logger.warning("Discovery message edit failed: %s", exc)
                            continue
                    self._last[job_id] = (body, status)
                    if status in FINISHED:
                        async with self.sessions() as session:
                            await session.execute(update(DiscoveryJob).where(
                                DiscoveryJob.id == job_id,
                                DiscoveryJob.status == status,
                            ).values(notified=True))
                            await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("discovery notification failed")
            await asyncio.sleep(10)
