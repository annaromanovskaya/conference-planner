from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from conference_scout.ai.gateway import ModelGatewayError
from conference_scout.ai.service import AIResponseError
from conference_scout.bot.keyboards import (
    action_confirmation,
    candidate_decision_actions,
    candidate_review_actions,
    conference_actions,
)
from conference_scout.conferences.monitoring import MonitoringCheckError
from conference_scout.conferences.research import (
    CandidateExpiredError,
    CandidateImportedError,
    CandidateRejectedError,
    CandidateSkippedError,
    CandidateUnusableError,
    CatalogExpansion,
)
from conference_scout.schemas.agent import ImportConferenceCandidateAction
from conference_scout.schemas.common import (
    CandidateStatus,
    ConferenceStatus,
    VerificationStatus,
)
from conference_scout.schemas.research import VerifiedConferenceResearch
from conference_scout.web.fetcher import FetchError

_RU_MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

_RU_MONTHS_NOMINATIVE = (
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)

_DEADLINE_LABELS = {
    "ABSTRACT_SUBMISSION": "Подача абстрактов",
    "ORAL_ABSTRACT_SUBMISSION": "Подача абстрактов для устных докладов",
    "POSTER_SUBMISSION": "Подача постеров",
    "REGISTRATION_OPEN": "Открытие регистрации",
    "EARLY_REGISTRATION": "Окончание ранней регистрации",
    "REGISTRATION": "Окончание регистрации",
    "ACCEPTANCE_NOTIFICATION": "Уведомление о принятии",
    "PAYMENT": "Оплата",
    "OTHER": "Другой дедлайн",
}


def candidate_card(candidate) -> str:
    summary = f"\n{escape(candidate.summary)}" if candidate.summary else ""
    start_date = getattr(candidate, "start_date", None)
    end_date = getattr(candidate, "end_date", None)
    location = getattr(candidate, "location", None)
    score = getattr(candidate, "topic_relevance_score", None)
    reason = getattr(candidate, "topic_relevance_reason", None)
    official_url = getattr(candidate, "official_url", None)
    research = _candidate_snapshot(candidate)
    deadline_lines = (
        "\n".join(_research_deadline_text(item) for item in research.deadlines)
        if research is not None and research.deadlines
        else "не найдены"
    )
    relevance = "не проверена"
    if score is not None:
        relevance = f"{score:.0%}"
        if reason:
            relevance += f" — {escape(reason)}"
    return "\n".join(
        (
            f"{escape(candidate.title)}{summary}",
            f"Время проведения: {_date_range(start_date, end_date, unknown='не проверено')}",
            f"Место: {escape(location) if location else 'не найдено'}",
            f"Тематическая релевантность: {relevance}",
            f"Дедлайны: {deadline_lines}",
            f"Официальная ссылка: {_html_link(official_url) if official_url else 'не найдена'}",
            f"Статус проверки: {escape(candidate.verification_status.value)}",
            f"Источник: {_html_link(candidate.source_url)}",
        )
    )


def _candidate_snapshot(candidate) -> VerifiedConferenceResearch | None:
    payload = getattr(candidate, "research_payload", None)
    if not payload:
        return None
    try:
        return VerifiedConferenceResearch.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        return None


def _date_range(start_date, end_date, *, unknown: str = "не указано") -> str:
    if start_date is not None and end_date is not None:
        return f"{start_date.isoformat()} — {end_date.isoformat()}"
    if start_date is not None:
        return start_date.isoformat()
    if end_date is not None:
        return f"до {end_date.isoformat()}"
    return unknown


def _html_link(url: str) -> str:
    safe_url = escape(str(url), quote=True)
    return f'<a href="{safe_url}">{safe_url}</a>'


def conference_lists_text(tracked, candidates, archived) -> str:
    def conferences(values) -> str:
        lines = []
        for item in values:
            dates = _date_range(
                getattr(item, "start_date", None), getattr(item, "end_date", None)
            )
            url = getattr(item, "website", None) or getattr(item, "source_url", None)
            link = _html_link(url) if url else "ссылка не указана"
            lines.append(f"{item.id}. {escape(item.name)}\n   {dates}\n   {link}")
        return "\n".join(lines) or "—"

    return (
        f"ОТСЛЕЖИВАЕМЫЕ\n{conferences(tracked)}\n\n"
        f"КАНДИДАТЫ\nОжидают рассмотрения: {len(candidates)}\n\n"
        f"АРХИВ\n{conferences(archived)}"
    )


async def send_next_candidate(message, repository, owner_user_id: int) -> None:
    candidates = await repository.list_candidates(
        owner_user_id, CandidateStatus.ACTIVE, limit=1
    )
    if not candidates:
        await message.answer("Новых кандидатов для рассмотрения нет.")
        return
    candidate = candidates[0]
    await message.answer(
        "СЛЕДУЮЩИЙ КАНДИДАТ\n\n" + candidate_card(candidate),
        reply_markup=candidate_review_actions(candidate.id, candidate.source_url),
        parse_mode="HTML",
    )


async def handle_next_candidate_message(message, state, repository) -> None:
    await state.clear()
    await send_next_candidate(message, repository, repository.owner_user_id)


async def send_conference_overview(
    message,
    tracked,
    candidates,
    archived,
    candidate_repository,
    *,
    owner_user_id: int,
) -> None:
    await message.answer(
        conference_lists_text(tracked, candidates, archived), parse_mode="HTML"
    )
    await send_next_candidate(message, candidate_repository, owner_user_id)


async def handle_candidate_details(
    callback, conference_research_service, owner_user_id: int, candidate_id: int
) -> None:
    try:
        research = await conference_research_service.inspect(owner_user_id, candidate_id)
        if isinstance(research, CatalogExpansion):
            await callback.message.answer(
                f"Каталог разобран. Найдено отдельных конференций: "
                f"{len(research.created_candidate_ids)}."
            )
            await send_next_candidate(
                callback.message,
                conference_research_service.repository,
                owner_user_id,
            )
            await callback.answer()
            return
        candidate = await conference_research_service.repository.get_candidate(
            owner_user_id, candidate_id
        )
    except CandidateUnusableError:
        await callback.answer(
            "На странице и в поисковой карточке не найдены даты проведения или "
            "дедлайны с цитируемым источником. "
            "Кандидат не сохранён и остался в очереди.",
            show_alert=True,
        )
        return
    except ValueError as exc:
        await callback.answer(stale_candidate_message(exc), show_alert=True)
        return
    except FetchError as exc:
        await callback.answer(
            f"Не удалось проверить страницу: {exc.user_message} "
            "Кандидат остался в очереди; можно открыть сайт через «Подробнее» "
            "или перейти к следующему.",
            show_alert=True,
        )
        return
    except (AIResponseError, ModelGatewayError):
        await callback.answer(
            "Не удалось проверить страницу кандидата. Он остался в очереди; "
            "можно открыть сайт через «Подробнее» или повторить проверку позже.",
            show_alert=True,
        )
        return
    text = candidate_research_text(research)
    markup = (
        candidate_decision_actions(candidate.id, candidate.source_url)
        if candidate_research_is_usable(research)
        else candidate_review_actions(candidate.id, candidate.source_url)
    )
    await callback.message.answer(
        candidate_card(candidate) + "\n\n" + text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


def candidate_research_is_usable(research) -> bool:
    return any(
        item.kind.casefold() in {"start_date", "end_date", "event_date", "event_dates"}
        for item in research.facts
    ) or bool(research.deadlines)


def candidate_research_text(research) -> str:
    claims = (*research.facts, *research.deadlines)
    confirmed = any(
        item.verification_status
        in {
            VerificationStatus.OFFICIAL_CONFIRMED,
            VerificationStatus.ORGANIZER_CONFIRMED,
        }
        for item in claims
    )
    heading = "ПРОВЕРЕННЫЕ ДАННЫЕ" if confirmed else "ИЗВЛЕЧЁННЫЕ ДАННЫЕ"
    event_kinds = {"start_date", "end_date", "event_date", "event_dates"}
    event_dates = [item for item in research.facts if item.kind.casefold() in event_kinds]
    other_facts = [item for item in research.facts if item.kind.casefold() not in event_kinds]
    date_lines = "\n".join(
        f"{escape(item.kind)}: {escape(item.value)}" for item in event_dates
    ) or "—"
    fact_lines = "\n".join(
        f"{escape(item.kind)}: {escape(item.value)}" for item in other_facts
    )
    deadline_lines = "\n".join(_research_deadline_text(item) for item in research.deadlines) or "—"
    sections = [heading, f"ДАТЫ ПРОВЕДЕНИЯ\n{date_lines}"]
    if fact_lines:
        sections.append(f"ДРУГИЕ ДАННЫЕ\n{fact_lines}")
    sections.append(f"ДЕДЛАЙНЫ\n{deadline_lines}")
    if research.verifier_notes:
        notes = "\n".join(escape(item) for item in research.verifier_notes)
        sections.append(f"ПРОБЛЕМЫ ПРОВЕРКИ\n{notes}")
    return "\n\n".join(sections)


def _research_deadline_text(deadline) -> str:
    if deadline.exact_date is not None:
        when = deadline.exact_date.isoformat()
    else:
        when = str(deadline.approximate_year)
        if deadline.approximate_month is not None:
            when += f"-{deadline.approximate_month:02d}"
        labels = ["ориентировочно"]
        if deadline.tentative:
            labels.append("предварительно")
        when += f" ({', '.join(labels)})"
    return f"{escape(deadline.deadline_type.value)}: {escape(when)}"


def _deadline_text(deadline) -> str:
    if deadline.deadline_date is not None:
        when = deadline.deadline_date.isoformat()
    else:
        when = str(deadline.approximate_year)
        if deadline.approximate_month is not None:
            when += f"-{deadline.approximate_month:02d}"
        labels = ["ориентировочно"]
        if deadline.tentative:
            labels.append("предварительно")
        when += f" ({', '.join(labels)})"
    deadline_type = (
        deadline.deadline_type.value
        if hasattr(deadline.deadline_type, "value")
        else deadline.deadline_type
    )
    return f"{deadline_type}: {when}"


def conference_details_text(conference, deadlines, evidence, history) -> str:
    deadline_lines = "\n".join(_deadline_text(item) for item in deadlines) or "—"
    source_lines = (
        "\n".join(f"{escape(item.fact_kind)}: {escape(item.source_url)}" for item in evidence)
        or "—"
    )
    history_lines = (
        "\n".join(
            f"{escape(item.field_name)}: {escape(item.old_value or '—')} → "
            f"{escape(item.new_value or '—')}"
            for item in history
        )
        or "—"
    )
    location = escape(conference.location) if conference.location else "не указано"
    official_url = getattr(conference, "website", None) or getattr(
        conference, "source_url", None
    )
    last_checked = getattr(conference, "last_checked_at", None)
    last_checked_text = (
        last_checked.strftime("%Y-%m-%d %H:%M") if last_checked else "ещё не проверялась"
    )
    event_dates = _date_range(
        getattr(conference, "start_date", None), getattr(conference, "end_date", None)
    )
    return (
        f"{escape(conference.name)}\n"
        f"Время проведения: {event_dates}\n"
        f"Место: {location}\n"
        f"Официальная ссылка: {_html_link(official_url) if official_url else 'не указана'}\n"
        f"Последняя успешная проверка: {last_checked_text}\n\n"
        f"ДЕДЛАЙНЫ\n{deadline_lines}\n\n"
        f"ИСТОЧНИКИ\n{source_lines}\n\n"
        f"ИСТОРИЯ\n{history_lines}"
    )


def deadline_list_text(values) -> str:
    grouped = {}
    for item in values:
        grouped.setdefault(item.conference_name, []).append(item)
    if not grouped:
        return "Будущих дедлайнов пока нет."

    sections = []
    for conference_name, deadlines in grouped.items():
        lines = [f"<b>{escape(conference_name)}</b>"]
        lines.extend(_human_deadline_text(item) for item in deadlines)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _human_deadline_text(deadline) -> str:
    deadline_type = (
        deadline.deadline_type.value
        if hasattr(deadline.deadline_type, "value")
        else deadline.deadline_type
    )
    label = _DEADLINE_LABELS.get(deadline_type, "Дедлайн")
    if deadline.deadline_date is not None:
        value = deadline.deadline_date
        when = f"{value.day} {_RU_MONTHS[value.month]} {value.year}"
    else:
        year = deadline.approximate_year
        month = deadline.approximate_month
        if year is None:
            when = "дата не указана"
        elif month is None:
            when = f"{year} год"
        else:
            when = f"{_RU_MONTHS_NOMINATIVE[month]} {year}"
    return f"{label}: {when}"


def stale_candidate_message(error: Exception) -> str:
    if isinstance(error, CandidateExpiredError):
        return "Срок действия кандидата истёк. Запустите поиск снова."
    if isinstance(error, CandidateSkippedError):
        return "Этот кандидат был пропущен."
    if isinstance(error, CandidateRejectedError):
        return "Этот кандидат был отклонён."
    if isinstance(error, CandidateImportedError):
        return "Этот кандидат уже добавлен."
    return "Кандидат не найден или больше недоступен."


async def handle_manual_check(callback, monitoring_service, owner_user_id: int, conference_id: int):
    await callback.answer("Проверяю конференцию…")
    try:
        receipt = await monitoring_service.check_conference(owner_user_id, conference_id)
    except (MonitoringCheckError, ValueError):
        await callback.message.answer("Проверка не удалась. Данные не изменены. Попробуйте позже.")
        return
    await callback.message.answer(receipt.telegram_summary())


def build_conference_router() -> Router:
    router = Router(name="conference-views")

    from conference_scout.bot.router import is_next_candidate_text

    @router.message(F.text.func(is_next_candidate_text))
    async def next_candidate(
        message: Message,
        state: FSMContext,
        conference_research_repository,
    ) -> None:
        await handle_next_candidate_message(
            message,
            state,
            conference_research_repository,
        )

    @router.message(F.text == "Конференции")
    async def conference_list(
        message: Message,
        state: FSMContext,
        conference_repository,
        conference_research_repository,
    ) -> None:
        await state.clear()
        owner_id = conference_repository.owner_user_id
        tracked = await conference_repository.list_accessible_by_status(
            owner_id, ConferenceStatus.TRACKING
        )
        archived = await conference_repository.list_accessible_by_status(
            owner_id, ConferenceStatus.ARCHIVED
        )
        candidates = await conference_research_repository.list_candidates(
            owner_id, CandidateStatus.ACTIVE, limit=100
        )
        await send_conference_overview(
            message,
            tracked,
            candidates,
            archived,
            conference_research_repository,
            owner_user_id=owner_id,
        )

    @router.message(F.text == "Ближайшие дедлайны")
    async def deadlines(
        message: Message, state: FSMContext, reminder_repository, application_clock
    ) -> None:
        await state.clear()
        values = await reminder_repository.upcoming(
            reminder_repository.owner_user_id, application_clock.today()
        )
        await message.answer(deadline_list_text(values), parse_mode="HTML")

    @router.callback_query(F.data.startswith("candidate:track:"))
    @router.callback_query(F.data.startswith("candidate:add:"))
    async def candidate_track(
        callback: CallbackQuery,
        confirmation_service,
        conference_research_service,
        owner_user_id: int,
    ) -> None:
        candidate_id = int(callback.data.rsplit(":", 1)[1])
        try:
            await conference_research_service.prepare_for_tracking(owner_user_id, candidate_id)
            pending = await confirmation_service.propose(
                owner_user_id, ImportConferenceCandidateAction(candidate_id=candidate_id)
            )
        except CandidateUnusableError:
            await callback.answer(
                "Не найдены подтверждённые даты проведения или дедлайны. "
                "Кандидат остался в очереди.",
                show_alert=True,
            )
            return
        except FetchError as exc:
            await callback.answer(
                f"Не удалось открыть страницу: {exc.user_message} "
                "Кандидат остался в очереди.",
                show_alert=True,
            )
            return
        except (AIResponseError, ModelGatewayError):
            await callback.answer(
                "Модель не смогла проверить кандидата. Он остался в очереди.",
                show_alert=True,
            )
            return
        except ValueError as exc:
            await callback.answer(stale_candidate_message(exc), show_alert=True)
            return
        await callback.message.answer(pending.summary, reply_markup=action_confirmation(pending.id))
        await callback.answer()

    @router.callback_query(F.data.startswith("candidate:details:"))
    async def candidate_details(
        callback: CallbackQuery, conference_research_service, owner_user_id: int
    ) -> None:
        candidate_id = int(callback.data.rsplit(":", 1)[1])
        await handle_candidate_details(
            callback, conference_research_service, owner_user_id, candidate_id
        )

    @router.callback_query(F.data.startswith("candidate:defer:"))
    async def candidate_defer(
        callback: CallbackQuery, conference_research_service, owner_user_id: int
    ) -> None:
        candidate_id = int(callback.data.rsplit(":", 1)[1])
        try:
            await conference_research_service.defer(owner_user_id, candidate_id)
        except ValueError as exc:
            await callback.answer(stale_candidate_message(exc), show_alert=True)
            return
        await callback.answer("Кандидат пропущен.")
        await send_next_candidate(
            callback.message, conference_research_service.repository, owner_user_id
        )

    @router.callback_query(F.data.startswith("candidate:reject:"))
    @router.callback_query(F.data.startswith("candidate:skip:"))
    async def candidate_reject(
        callback: CallbackQuery, conference_research_service, owner_user_id: int
    ) -> None:
        candidate_id = int(callback.data.rsplit(":", 1)[1])
        try:
            await conference_research_service.reject(owner_user_id, candidate_id)
        except ValueError as exc:
            await callback.answer(stale_candidate_message(exc), show_alert=True)
            return
        await callback.answer("Кандидат отклонён.")
        await send_next_candidate(
            callback.message, conference_research_service.repository, owner_user_id
        )

    @router.callback_query(F.data.startswith("conference:details:"))
    async def conference_details(
        callback: CallbackQuery,
        conference_repository,
        conference_research_repository,
        owner_user_id: int,
    ) -> None:
        conference_id = int(callback.data.rsplit(":", 1)[1])
        conference = await conference_repository.require_accessible(owner_user_id, conference_id)
        deadlines = await conference_repository.list_deadlines(conference_id)
        evidence = await conference_research_repository.list_evidence(conference_id)
        history = await conference_research_repository.list_changes(conference_id)
        await callback.message.answer(
            conference_details_text(conference, deadlines, evidence, history),
            reply_markup=conference_actions(
                conference_id, conference.website or conference.source_url
            ),
            parse_mode="HTML",
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("conference:history:"))
    async def conference_history(
        callback: CallbackQuery,
        conference_repository,
        conference_research_repository,
        owner_user_id: int,
    ) -> None:
        conference_id = int(callback.data.rsplit(":", 1)[1])
        await conference_repository.require_accessible(owner_user_id, conference_id)
        values = await conference_research_repository.list_changes(conference_id)
        text = (
            "\n".join(
                f"{item.field_name}: {item.old_value or '—'} → {item.new_value or '—'}"
                for item in values
            )
            or "История изменений пока пуста."
        )
        await callback.message.answer(text)
        await callback.answer()

    @router.callback_query(F.data.startswith("conference:check:"))
    async def conference_check(
        callback: CallbackQuery, monitoring_service, owner_user_id: int
    ) -> None:
        conference_id = int(callback.data.rsplit(":", 1)[1])
        await handle_manual_check(callback, monitoring_service, owner_user_id, conference_id)

    return router
