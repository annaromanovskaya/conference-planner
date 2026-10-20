from datetime import date
from types import SimpleNamespace

import pytest

from conference_scout.bot.conference_router import (
    candidate_card,
    candidate_research_text,
    conference_details_text,
    conference_lists_text,
    deadline_list_text,
    handle_candidate_details,
    handle_manual_check,
    handle_next_candidate_message,
    send_conference_overview,
    send_next_candidate,
    stale_candidate_message,
)
from conference_scout.bot.keyboards import (
    candidate_decision_actions,
    candidate_review_actions,
    conference_actions,
)
from conference_scout.conferences.research import (
    CandidateExpiredError,
    CandidateImportedError,
    CandidateSkippedError,
    CatalogExpansion,
)
from conference_scout.schemas.research import VerifiedConferenceResearch


def test_candidate_card_escapes_untrusted_text_and_review_buttons_use_durable_id() -> None:
    candidate = SimpleNamespace(
        id=17,
        title="Stars <script>",
        source_url="https://example.org/?a=1&b=2",
        summary="Binary & stellar meeting",
        verification_status=SimpleNamespace(value="OFFICIAL_CONFIRMED"),
    )

    text = candidate_card(candidate)
    markup = candidate_review_actions(candidate.id, candidate.source_url)
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    urls = [button.url for row in markup.inline_keyboard for button in row if button.url]

    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "Binary &amp; stellar" in text
    assert "Время проведения: не проверено" in text
    assert "Место: не найдено" in text
    assert "Дедлайны: не найдены" in text
    assert callbacks == ["candidate:details:17", "candidate:skip:17"]
    assert urls == ["https://example.org/?a=1&b=2"]

    decision_markup = candidate_decision_actions(candidate.id, candidate.source_url)
    decision_callbacks = [
        button.callback_data
        for row in decision_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert decision_callbacks == ["candidate:add:17", "candidate:skip:17"]


def test_verified_candidate_card_shows_snapshot_dates_place_relevance_and_deadlines() -> None:
    research = _candidate_research(confirmed=True)
    candidate = SimpleNamespace(
        id=18,
        title="Binary Stars <2027>",
        source_url="https://catalog.example/?a=1&b=2",
        summary="Stellar & ML",
        verification_status=SimpleNamespace(value="OFFICIAL_CONFIRMED"),
        start_date=date(2027, 7, 12),
        end_date=date(2027, 7, 16),
        location="Tenerife & La Laguna",
        official_url="https://event.example/?a=1&b=2",
        topic_relevance_score=0.91,
        topic_relevance_reason="Спектроскопия & ML",
        research_payload=research.model_dump(mode="json"),
    )

    text = candidate_card(candidate)

    assert "Binary Stars &lt;2027&gt;" in text
    assert "2027-07-12 — 2027-07-16" in text
    assert "Tenerife &amp; La Laguna" in text
    assert "91% — Спектроскопия &amp; ML" in text
    assert "ABSTRACT_SUBMISSION: 2027-02-12" in text
    assert "REGISTRATION: 2027-05 (ориентировочно, предварительно)" in text
    assert 'href="https://event.example/?a=1&amp;b=2"' in text


def test_conference_lists_and_details_are_database_backed_plain_sections() -> None:
    tracked = [
        SimpleNamespace(
            id=1,
            name="IMBASE 2027",
            start_date=date(2027, 7, 12),
            end_date=date(2027, 7, 16),
            website=None,
            source_url="https://meetings.example/?a=1&b=2",
        )
    ]
    candidates = [SimpleNamespace(id=7, title="IAU <2027>")]
    archived = [SimpleNamespace(id=3, name="Old meeting")]

    text = conference_lists_text(tracked, candidates, archived)
    details = conference_details_text(
        SimpleNamespace(
            id=1,
            name="IMBASE 2027",
            location="Tenerife",
            website="https://imbase.example/?a=1&b=2",
            source_url="https://source.example/",
            start_date=date(2027, 7, 12),
            end_date=date(2027, 7, 16),
            last_checked_at=SimpleNamespace(strftime=lambda _format: "2026-10-19 11:30"),
        ),
        [
            SimpleNamespace(
                deadline_type=SimpleNamespace(value="REGISTRATION"),
                deadline_date=None,
                approximate_year=2027,
                approximate_month=5,
                tentative=True,
                source_url="https://example.org/dates",
            )
        ],
        [SimpleNamespace(source_url="https://example.org/dates", fact_kind="location")],
        [SimpleNamespace(field_name="location", old_value="Madrid", new_value="Tenerife")],
    )

    assert "ОТСЛЕЖИВАЕМЫЕ" in text
    assert "КАНДИДАТЫ" in text
    assert "АРХИВ" in text
    assert "Ожидают рассмотрения: 1" in text
    assert "2027-07-12 — 2027-07-16" in text
    assert 'href="https://meetings.example/?a=1&amp;b=2"' in text
    assert "ориентировочно" in details
    assert 'href="https://imbase.example/?a=1&amp;b=2"' in details
    assert "Последняя успешная проверка: 2026-10-19 11:30" in details
    assert "ИСТОЧНИКИ" in details
    assert "ИСТОРИЯ" in details
    callbacks = [
        button.callback_data for row in conference_actions(1).inline_keyboard for button in row
    ]
    assert callbacks == ["conference:check:1", "conference:history:1"]


def test_deadline_list_groups_conferences_and_formats_dates_in_russian() -> None:
    values = [
        SimpleNamespace(
            conference_name="XXXIII IAU General Assembly",
            deadline_type="ABSTRACT_SUBMISSION",
            deadline_date=date(2027, 1, 15),
            approximate_year=None,
            approximate_month=None,
            tentative=False,
        ),
        SimpleNamespace(
            conference_name="XXXIII IAU General Assembly",
            deadline_type="REGISTRATION",
            deadline_date=date(2027, 2, 17),
            approximate_year=None,
            approximate_month=None,
            tentative=False,
        ),
        SimpleNamespace(
            conference_name="IMBASE 2027 <official>",
            deadline_type="REGISTRATION_OPEN",
            deadline_date=None,
            approximate_year=2026,
            approximate_month=11,
            tentative=True,
        ),
        SimpleNamespace(
            conference_name="IMBASE 2027 <official>",
            deadline_type="ORAL_ABSTRACT_SUBMISSION",
            deadline_date=None,
            approximate_year=2027,
            approximate_month=1,
            tentative=True,
        ),
    ]

    text = deadline_list_text(values)

    assert text == (
        "<b>XXXIII IAU General Assembly</b>\n"
        "Подача абстрактов: 15 января 2027\n"
        "Окончание регистрации: 17 февраля 2027\n\n"
        "<b>IMBASE 2027 &lt;official&gt;</b>\n"
        "Открытие регистрации: ноябрь 2026\n"
        "Подача абстрактов для устных докладов: январь 2027"
    )
    assert "ориентировочно" not in text
    assert "предварительно" not in text


def test_stale_candidate_errors_are_precise() -> None:
    assert "истёк" in stale_candidate_message(CandidateExpiredError()).casefold()
    assert "пропущен" in stale_candidate_message(CandidateSkippedError()).casefold()
    assert "уже добавлен" in stale_candidate_message(CandidateImportedError()).casefold()
    assert "не найден" in stale_candidate_message(ValueError()).casefold()


async def _raise_check_error(*_args):
    from conference_scout.conferences.monitoring import MonitoringCheckError

    raise MonitoringCheckError("secret upstream body")


async def _receipt_check(*_args):
    return SimpleNamespace(telegram_summary=lambda: "Обновлено: start_date.")


class _Callback:
    def __init__(self):
        self.alerts = []
        self.messages = []
        self.message_markup = None
        self.message = SimpleNamespace(answer=self._answer)

    async def answer(self, text=None, **kwargs):
        self.alerts.append((text, kwargs))

    async def _answer(self, text, **kwargs):
        self.messages.append(text)
        self.message_markup = kwargs.get("reply_markup")


@pytest.mark.asyncio
async def test_manual_check_acknowledges_progress_and_uses_receipt() -> None:
    callback = _Callback()
    service = SimpleNamespace(check_conference=_receipt_check)

    await handle_manual_check(callback, service, 7, 11)

    assert callback.alerts[0][0] == "Проверяю конференцию…"
    assert callback.messages == ["Обновлено: start_date."]


@pytest.mark.asyncio
async def test_manual_check_sanitizes_failure() -> None:
    callback = _Callback()
    service = SimpleNamespace(check_conference=_raise_check_error)

    await handle_manual_check(callback, service, 7, 11)

    assert "secret" not in callback.messages[0]
    assert "Данные не изменены" in callback.messages[0]


@pytest.mark.asyncio
async def test_candidate_queue_sends_only_first_active_candidate() -> None:
    candidates = [
        SimpleNamespace(
            id=7,
            title="First candidate",
            source_url="https://first.example/events",
            summary="First",
            verification_status=SimpleNamespace(value="UNVERIFIABLE"),
        ),
        SimpleNamespace(
            id=8,
            title="Second candidate",
            source_url="https://second.example/events",
            summary="Second",
            verification_status=SimpleNamespace(value="UNVERIFIABLE"),
        ),
    ]

    class Repository:
        async def list_candidates(self, user_id, status, *, limit):
            assert user_id == 7
            assert limit == 1
            return candidates[:limit]

    class Message:
        def __init__(self):
            self.answers = []

        async def answer(self, text, **kwargs):
            self.answers.append((text, kwargs))

    message = Message()
    await send_next_candidate(message, Repository(), 7)

    assert len(message.answers) == 1
    assert "First candidate" in message.answers[0][0]
    assert "Second candidate" not in message.answers[0][0]


@pytest.mark.asyncio
async def test_conference_overview_does_not_send_every_tracked_conference_as_a_card() -> None:
    tracked = [
        SimpleNamespace(id=1, name="First tracked"),
        SimpleNamespace(id=2, name="Second tracked"),
    ]
    candidate = SimpleNamespace(
        id=7,
        title="Only current candidate",
        source_url="https://candidate.example/event",
        summary="Candidate summary",
        verification_status=SimpleNamespace(value="UNVERIFIABLE"),
    )

    class Repository:
        async def list_candidates(self, _user_id, _status, *, limit):
            return [candidate][:limit]

    class Message:
        def __init__(self):
            self.answers = []

        async def answer(self, text, **kwargs):
            self.answers.append((text, kwargs))

    message = Message()
    await send_conference_overview(
        message,
        tracked,
        [candidate],
        [],
        Repository(),
        owner_user_id=7,
    )

    assert len(message.answers) == 2
    assert "First tracked" in message.answers[0][0]
    assert "Second tracked" in message.answers[0][0]
    assert "Only current candidate" in message.answers[1][0]
    callbacks = [
        button.callback_data
        for row in message.answers[1][1]["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert callbacks == ["candidate:details:7", "candidate:skip:7"]


@pytest.mark.asyncio
async def test_candidate_check_failure_keeps_candidate_and_reports_precise_error() -> None:
    from conference_scout.web.fetcher import FetchError

    class Service:
        async def inspect(self, _user_id, _candidate_id):
            raise FetchError("secret network details")

    callback = _Callback()

    await handle_candidate_details(callback, Service(), 7, 11)

    assert callback.messages == []
    assert callback.alerts[-1][1]["show_alert"] is True
    assert "не удалось проверить" in callback.alerts[-1][0].casefold()
    assert "secret" not in callback.alerts[-1][0]


def _candidate_research(*, confirmed: bool, usable: bool = True):
    status = "OFFICIAL_CONFIRMED" if confirmed else "UNVERIFIABLE"
    facts = []
    if usable:
        facts = [
            {
                "kind": "start_date",
                "value": "2027-07-12",
                "source_url": "https://example.org/catalog",
                "source_class": "OFFICIAL_CONFERENCE",
                "excerpt": "12-16 July 2027",
                "verification_status": status,
            },
            {
                "kind": "end_date",
                "value": "2027-07-16",
                "source_url": "https://example.org/catalog",
                "source_class": "OFFICIAL_CONFERENCE",
                "excerpt": "12-16 July 2027",
                "verification_status": status,
            },
        ]
    return VerifiedConferenceResearch.model_validate(
        {
            "name": "Binary Stars 2027",
            "source_url": "https://example.org/catalog",
            "source_class": "OFFICIAL_CONFERENCE",
            "facts": facts,
            "deadlines": [
                {
                    "deadline_type": "ABSTRACT_SUBMISSION",
                    "exact_date": "2027-02-12",
                    "source_url": "https://example.org/catalog",
                    "source_class": "OFFICIAL_CONFERENCE",
                    "excerpt": "Abstract deadline: 12 February 2027",
                    "verification_status": status,
                },
                {
                    "deadline_type": "REGISTRATION",
                    "approximate_year": 2027,
                    "approximate_month": 5,
                    "tentative": True,
                    "source_url": "https://example.org/catalog",
                    "source_class": "OFFICIAL_CONFERENCE",
                    "excerpt": "May 2027 registration deadline (tentative)",
                    "verification_status": "TENTATIVE",
                },
            ]
            if usable
            else [],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confirmed", "expected_heading"),
    [(False, "ИЗВЛЕЧЁННЫЕ ДАННЫЕ"), (True, "ПРОВЕРЕННЫЕ ДАННЫЕ")],
)
async def test_candidate_details_separates_event_dates_and_deadlines(
    confirmed: bool, expected_heading: str
) -> None:
    research = _candidate_research(confirmed=confirmed)
    candidate = SimpleNamespace(
        id=7,
        title=research.name,
        source_url=str(research.source_url),
        summary=None,
        verification_status=SimpleNamespace(
            value="OFFICIAL_CONFIRMED" if confirmed else "UNVERIFIABLE"
        ),
    )

    class Repository:
        async def get_candidate(self, _user_id, _candidate_id):
            return candidate

    class Service:
        repository = Repository()

        async def inspect(self, _user_id, _candidate_id):
            return research

    callback = _Callback()
    await handle_candidate_details(callback, Service(), 1, candidate.id)

    text = callback.messages[0]
    assert expected_heading in text
    assert "ДАТЫ ПРОВЕДЕНИЯ" in text
    assert "start_date: 2027-07-12" in text
    assert "ДЕДЛАЙНЫ" in text
    assert "ABSTRACT_SUBMISSION: 2027-02-12" in text
    assert "REGISTRATION: 2027-05 (ориентировочно, предварительно)" in text
    markup = callback.message_markup
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "candidate:add:7" in callbacks


def test_candidate_research_text_reports_unavailable_related_pages() -> None:
    research = _candidate_research(confirmed=True).model_copy(
        update={
            "verifier_notes": (
                "Не удалось открыть связанную страницу "
                "https://example.org/important-dates: HTTP_403",
            )
        }
    )

    text = candidate_research_text(research)

    assert "ПРОБЛЕМЫ ПРОВЕРКИ" in text
    assert "https://example.org/important-dates: HTTP_403" in text


@pytest.mark.asyncio
async def test_catalog_expansion_reports_count_and_sends_only_first_child() -> None:
    first = SimpleNamespace(
        id=8,
        title="First extracted event",
        source_url="https://example.org/catalog",
        summary="Dates",
        verification_status=SimpleNamespace(value="UNVERIFIABLE"),
    )

    class Repository:
        async def list_candidates(self, _user_id, _status, *, limit):
            return [first][:limit]

    class Service:
        repository = Repository()

        async def inspect(self, _user_id, _candidate_id):
            return CatalogExpansion(7, (8, 9))

    callback = _Callback()
    await handle_candidate_details(callback, Service(), 1, 7)

    assert len(callback.messages) == 2
    assert "Найдено отдельных конференций: 2" in callback.messages[0]
    assert "First extracted event" in callback.messages[1]


@pytest.mark.asyncio
async def test_next_candidate_command_clears_state_and_bypasses_agent() -> None:
    candidate = SimpleNamespace(
        id=8,
        title="Next event",
        source_url="https://example.org/event",
        summary=None,
        verification_status=SimpleNamespace(value="UNVERIFIABLE"),
    )

    class State:
        cleared = False

        async def clear(self):
            self.cleared = True

    class Repository:
        owner_user_id = 1

        async def list_candidates(self, _user_id, _status, *, limit):
            return [candidate][:limit]

    class Message:
        def __init__(self):
            self.answers = []

        async def answer(self, text, **kwargs):
            self.answers.append((text, kwargs))

    state, message = State(), Message()
    await handle_next_candidate_message(message, state, Repository())

    assert state.cleared is True
    assert len(message.answers) == 1
    assert "Next event" in message.answers[0][0]
