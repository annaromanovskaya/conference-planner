from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.conferences.research import (
    CandidateExpiredError,
    CandidateImportedError,
    CandidateSkippedError,
    CandidateUnusableError,
    CatalogExpansion,
    ConferenceResearchService,
)
from conference_scout.db.migrations import run_migrations
from conference_scout.db.models import Conference, EventCandidate, Subscription
from conference_scout.db.repositories.conference_research import ConferenceResearchRepository
from conference_scout.db.repositories.reminders import ReminderRepository
from conference_scout.db.repositories.topics import TopicRepository
from conference_scout.db.repositories.users import UserRepository
from conference_scout.db.session import create_engine_and_sessionmaker
from conference_scout.runtime_clock import ApplicationClock
from conference_scout.schemas.common import CandidateDecisionReason, CandidateStatus
from conference_scout.schemas.integrations import SearchResult
from conference_scout.schemas.research import (
    ConferenceCatalogResearch,
    ConferenceResearch,
    VerifiedConferenceResearch,
)
from conference_scout.schemas.topics import RelevanceAssessment, TopicNormalization
from conference_scout.web.fetcher import FetchedPage, FetchError


class FakeSearch:
    def __init__(self) -> None:
        self.pages: list[int] = []

    async def search(self, _query: str, *, page: int = 1):
        self.pages.append(page)
        return [
            SearchResult(
                title="ImBaSE 2027 secondary listing",
                url="https://catalog.example/imbase-2027",
                snippet="Binary stars meeting",
            ),
            SearchResult(
                title="ImBaSE 2027",
                url="https://meetings.iac.es/imbase2027/",
                snippet="Official conference site",
            ),
            SearchResult(
                title="Unrelated Astro Meeting",
                url="https://other.example/astro",
                snippet="Another event",
            ),
        ]


class FakeFetcher:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def fetch(self, url: str, *, max_bytes: int):
        self.urls.append(url)
        return FetchedPage(url, f"<h1>{url}</h1><p>July 12-16, 2027</p>")


class FakeAI:
    def __init__(self, *, relevance_score: float = 0.9) -> None:
        self.extracted_urls: list[str] = []
        self.relevance_score = relevance_score

    async def extract_conference_research(self, source_url: str, _text: str, expected_name: str):
        self.extracted_urls.append(source_url)
        is_imbase = "imbase" in source_url
        official = "meetings.iac.es" in source_url
        name = "ImBaSE 2027" if is_imbase else "Unrelated Astro Meeting"
        source_class = "OFFICIAL_CONFERENCE" if official else "SECONDARY"
        status = "OFFICIAL_CONFIRMED" if official else "SECONDARY_UNVERIFIED"
        facts = []
        if is_imbase:
            facts = [
                {
                    "kind": "official_url",
                    "value": "https://meetings.iac.es/imbase2027/",
                    "source_url": source_url,
                    "source_class": source_class,
                    "excerpt": "ImBaSE 2027 official website",
                    "verification_status": status,
                },
                {
                    "kind": "start_date",
                    "value": "2027-07-12",
                    "source_url": source_url,
                    "source_class": source_class,
                    "excerpt": "July 12-16, 2027",
                    "verification_status": status,
                },
                {
                    "kind": "end_date",
                    "value": "2027-07-16",
                    "source_url": source_url,
                    "source_class": source_class,
                    "excerpt": "July 12-16, 2027",
                    "verification_status": status,
                },
            ]
        return ConferenceResearch.model_validate(
            {
                "name": name,
                "source_url": source_url,
                "source_class": source_class,
                "facts": facts,
                "deadlines": [],
            }
        )

    async def verify_conference_research(self, research: ConferenceResearch):
        return VerifiedConferenceResearch.model_validate(research.model_dump(mode="json"))

    async def rank_research_relevance(self, _research, _topics):
        return RelevanceAssessment(
            score=self.relevance_score,
            explanation=(
                "Соответствует темам" if self.relevance_score >= 0.5 else "Не соответствует"
            ),
        )


class CatalogSearch:
    async def search(self, _query: str, *, page: int = 1):
        return [
            SearchResult(
                title="Объявления о конференциях - ГАИШ - МГУ",
                url="https://www.sai.msu.ru/conference/annconf/index.html",
                snippet="Каталог астрономических конференций",
            )
        ]


def _catalog_event(name: str, start_date: str | None = None) -> ConferenceResearch:
    facts = []
    if start_date:
        facts.append(
            {
                "kind": "start_date",
                "value": start_date,
                "source_url": "https://www.sai.msu.ru/conference/annconf/index.html",
                "source_class": "SECONDARY",
                "excerpt": f"{name} состоится {start_date}",
                "verification_status": "UNVERIFIABLE",
            }
        )
    return ConferenceResearch.model_validate(
        {
            "name": name,
            "source_url": "https://www.sai.msu.ru/conference/annconf/index.html",
            "source_class": "SECONDARY",
            "facts": facts,
            "deadlines": [],
        }
    )


class CatalogAI(FakeAI):
    def __init__(self, events: tuple[ConferenceResearch, ...]) -> None:
        super().__init__()
        self.events = events

    async def extract_conference_catalog(self, source_url: str, _text: str):
        self.extracted_urls.append(source_url)
        return ConferenceCatalogResearch(events=self.events)


def _service(
    session: AsyncSession,
    *,
    search=None,
    fetcher=None,
    ai=None,
    application_clock=None,
    min_relevance_score: float = 0.5,
) -> ConferenceResearchService:
    return ConferenceResearchService(
        session,
        search or FakeSearch(),
        fetcher or FakeFetcher(),
        ai or FakeAI(),
        max_page_bytes=100_000,
        max_page_chars=20_000,
        candidate_ttl=timedelta(days=2),
        application_clock=application_clock,
        min_relevance_score=min_relevance_score,
    )


@pytest.mark.asyncio
async def test_search_stores_unverified_candidates_without_opening_every_site(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(501)
    search, fetcher, ai = FakeSearch(), FakeFetcher(), FakeAI()

    candidates = await _service(session, search=search, fetcher=fetcher, ai=ai).search(
        owner.id, "binary stars 2027", limit=2, page=4
    )

    assert len(candidates) == 2
    assert candidates[0].verification_status.value == "UNVERIFIABLE"
    assert candidates[0].id > 0
    assert await session.scalar(select(func.count()).select_from(EventCandidate)) == 2
    assert search.pages == [4]
    assert fetcher.urls == []
    assert ai.extracted_urls == []


@pytest.mark.asyncio
async def test_candidate_research_snapshot_round_trip_and_rejects_corrupt_json(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(509)
    candidate = (await _service(session).search(owner.id, "IMBASE", limit=1))[0]
    researched_at = datetime(2026, 10, 19, 8, 30, tzinfo=UTC)
    research = VerifiedConferenceResearch.model_validate(
        {
            "name": "ImBaSE 2027",
            "source_url": "https://meetings.iac.es/imbase2027/",
            "source_class": "OFFICIAL_CONFERENCE",
            "facts": [
                {
                    "kind": "start_date",
                    "value": "2027-07-12",
                    "source_url": "https://meetings.iac.es/imbase2027/",
                    "source_class": "OFFICIAL_CONFERENCE",
                    "excerpt": "12th of July 2027: ImBaSE2027 starts",
                    "verification_status": "OFFICIAL_CONFIRMED",
                },
                {
                    "kind": "end_date",
                    "value": "2027-07-16",
                    "source_url": "https://meetings.iac.es/imbase2027/",
                    "source_class": "OFFICIAL_CONFERENCE",
                    "excerpt": "July 12-16, 2027",
                    "verification_status": "OFFICIAL_CONFIRMED",
                },
                {
                    "kind": "location",
                    "value": "Tenerife, Canary Islands, Spain",
                    "source_url": "https://meetings.iac.es/imbase2027/",
                    "source_class": "OFFICIAL_CONFERENCE",
                    "excerpt": "Tenerife, Canary Islands, Spain",
                    "verification_status": "OFFICIAL_CONFIRMED",
                },
                {
                    "kind": "official_url",
                    "value": "https://meetings.iac.es/imbase2027/",
                    "source_url": "https://meetings.iac.es/imbase2027/",
                    "source_class": "OFFICIAL_CONFERENCE",
                    "excerpt": "Official meeting website",
                    "verification_status": "OFFICIAL_CONFIRMED",
                },
            ],
            "deadlines": [
                {
                    "deadline_type": "ABSTRACT_SUBMISSION",
                    "approximate_year": 2027,
                    "approximate_month": 1,
                    "tentative": True,
                    "source_url": "https://meetings.iac.es/imbase2027/pages/important-dates.php",
                    "source_class": "OFFICIAL_CONFERENCE",
                    "excerpt": "January 2027: abstract submission closes",
                    "verification_status": "OFFICIAL_CONFIRMED",
                }
            ],
        }
    )
    relevance = RelevanceAssessment(score=0.92, explanation="Звёздная астрофизика")
    repository = ConferenceResearchRepository(session)

    stored = await repository.save_candidate_snapshot(
        owner.id, candidate.id, research, relevance, researched_at
    )
    loaded = await repository.load_candidate_snapshot(owner.id, candidate.id)

    assert stored.start_date == date(2027, 7, 12)
    assert stored.end_date == date(2027, 7, 16)
    assert stored.location == "Tenerife, Canary Islands, Spain"
    assert stored.official_url == "https://meetings.iac.es/imbase2027/"
    assert stored.topic_relevance_score == 0.92
    assert stored.topic_relevance_reason == "Звёздная астрофизика"
    assert stored.researched_at == researched_at
    assert loaded == research

    stored.research_payload = {"invalid": "payload"}
    await session.flush()
    assert await repository.load_candidate_snapshot(owner.id, candidate.id) is None


@pytest.mark.asyncio
async def test_search_uses_local_date_and_drops_only_clearly_irrelevant_results(
    session: AsyncSession,
) -> None:
    class Search:
        def __init__(self) -> None:
            self.query = ""

        async def search(self, query: str, *, page: int = 1):
            self.query = query
            return [
                SearchResult(
                    title="Stellar spectroscopy 2027",
                    url="https://astro.example/stellar",
                    snippet="Machine learning in astronomy",
                ),
                SearchResult(
                    title="Marine biology symposium",
                    url="https://ocean.example/event",
                    snippet="Ocean ecology and fisheries",
                ),
            ]

    owner = await UserRepository(session).get_or_create_by_telegram_id(510)
    await TopicRepository(session).create(
        owner.id,
        TopicNormalization(
            name="Звёздная спектроскопия",
            description="Спектры звёзд и ML",
            keywords=["stellar spectroscopy", "machine learning astronomy"],
        ),
    )
    search = Search()
    clock = ApplicationClock.start(
        ZoneInfo("Europe/Moscow"),
        now_provider=lambda: datetime(2026, 10, 19, 8, 0, tzinfo=UTC),
    )

    candidates = await _service(
        session, search=search, application_clock=clock
    ).search(owner.id, "конференции 2026-2027", limit=10)

    assert "2026-10-19" in search.query
    assert [item.title for item in candidates] == ["Stellar spectroscopy 2027"]
    assert candidates[0].relevance_score == 1.0


@pytest.mark.asyncio
async def test_deep_check_skips_low_relevance_and_expires_past_event(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(511)
    await TopicRepository(session).create(
        owner.id,
        TopicNormalization(
            name="Двойные звёзды",
            description="Спектроскопические двойные",
            keywords=["binary stars"],
        ),
    )
    clock = ApplicationClock.start(
        ZoneInfo("Europe/Moscow"),
        now_provider=lambda: datetime(2026, 10, 19, 8, 0, tzinfo=UTC),
    )
    low_service = _service(
        session,
        ai=FakeAI(relevance_score=0.1),
        application_clock=clock,
        min_relevance_score=0.5,
    )
    low_candidate = (await low_service.search(owner.id, "binary stars", limit=1))[0]

    with pytest.raises(CandidateUnusableError, match="not relevant"):
        await low_service.inspect(owner.id, low_candidate.id)
    assert (await session.get(EventCandidate, low_candidate.id)).status.value == "SKIPPED"

    class PastAI(FakeAI):
        async def extract_conference_research(self, source_url, _text, expected_name):
            return ConferenceResearch.model_validate(
                {
                    "name": expected_name,
                    "source_url": source_url,
                    "source_class": "OFFICIAL_CONFERENCE",
                    "facts": [
                        {
                            "kind": "end_date",
                            "value": "2026-10-18",
                            "source_url": source_url,
                            "source_class": "OFFICIAL_CONFERENCE",
                            "excerpt": "Ends October 18, 2026",
                            "verification_status": "OFFICIAL_CONFIRMED",
                        }
                    ],
                }
                )

    class PastSearch:
        async def search(self, _query: str, *, page: int = 1):
            return [
                SearchResult(
                    title="Binary Stars Past Meeting",
                    url="https://past.example/binary-stars",
                    snippet="Binary stars conference",
                )
                ]

    class PastFetcher:
        async def fetch(self, url: str, *, max_bytes: int):
            return FetchedPage(url, "<p>Ends October 18, 2026</p>")

    past_service = _service(
        session,
        search=PastSearch(),
        fetcher=PastFetcher(),
        ai=PastAI(),
        application_clock=clock,
    )
    past_candidate = (await past_service.search(owner.id, "binary stars past", limit=1))[0]

    with pytest.raises(CandidateExpiredError, match="event already ended"):
        await past_service.inspect(owner.id, past_candidate.id)
    assert (await session.get(EventCandidate, past_candidate.id)).status.value == "EXPIRED"


@pytest.mark.asyncio
async def test_search_can_continue_past_twenty_sites_across_pages(
    session: AsyncSession,
) -> None:
    class PaginatedSearch:
        async def search(self, query: str, *, page: int = 1):
            return [
                SearchResult(
                    title=f"{query} result {page}-{index}",
                    url=f"https://events.example/{page}/{index}",
                    snippet="Conference candidate",
                )
                for index in range(15)
            ]

    owner = await UserRepository(session).get_or_create_by_telegram_id(503)
    service = _service(session, search=PaginatedSearch())

    first = await service.search(owner.id, "stellar astronomy", limit=50, page=1)
    second = await service.search(owner.id, "stellar astronomy", limit=50, page=2)

    assert len(first) == 15
    assert len(second) == 15
    assert await session.scalar(select(func.count()).select_from(EventCandidate)) == 30

    repeated = await service.search(owner.id, "stellar astronomy", limit=50, page=2)

    assert len(repeated) == 15
    assert service.last_search_new_count == 0


@pytest.mark.asyncio
async def test_fresh_search_continues_after_fully_known_first_page(
    session: AsyncSession,
) -> None:
    class Search:
        def __init__(self) -> None:
            self.pages: list[int] = []

        async def search(self, _query: str, *, page: int = 1):
            self.pages.append(page)
            if page == 1:
                return [
                    SearchResult(
                        title="Known stellar meeting",
                        url="https://events.example/known",
                    )
                ]
            if page == 2:
                return [
                    SearchResult(
                        title="New binary stars meeting",
                        url="https://events.example/new",
                    )
                ]
            return []

    owner = await UserRepository(session).get_or_create_by_telegram_id(5_030)
    search = Search()
    service = _service(session, search=search)
    await service.search(owner.id, "stellar astronomy", limit=10, page=1)
    search.pages.clear()

    report = await service.search_fresh(
        owner.id,
        "stellar astronomy",
        page_limit=3,
        results_per_page=10,
    )

    assert search.pages == [1, 2]
    assert report.pages_checked == 2
    assert len(report.new_ids) == 1
    assert report.new_ids[0] in report.active_ids
    assert report.duplicates == 1
    assert report.exhausted is False


@pytest.mark.asyncio
async def test_fresh_search_never_reactivates_rejected_candidate(
    session: AsyncSession,
) -> None:
    class Search:
        async def search(self, _query: str, *, page: int = 1):
            if page == 1:
                return [
                    SearchResult(
                        title="Rejected meeting",
                        url="https://events.example/rejected",
                    )
                ]
            if page == 2:
                return [
                    SearchResult(
                        title="Fresh meeting",
                        url="https://events.example/fresh",
                    )
                ]
            return []

    owner = await UserRepository(session).get_or_create_by_telegram_id(5_031)
    service = _service(session, search=Search())
    rejected = (await service.search(owner.id, "stars", limit=10, page=1))[0]
    await service.reject(owner.id, rejected.id)

    report = await service.search_fresh(
        owner.id,
        "stars",
        page_limit=3,
        results_per_page=10,
    )

    await session.refresh(rejected)
    assert rejected.status == CandidateStatus.REJECTED
    assert rejected.id not in report.active_ids
    assert len(report.new_ids) == 1


@pytest.mark.asyncio
async def test_fresh_search_with_no_new_candidates_is_successful(
    session: AsyncSession,
) -> None:
    class Search:
        async def search(self, _query: str, *, page: int = 1):
            if page == 1:
                return [
                    SearchResult(
                        title="Known meeting",
                        url="https://events.example/known-only",
                    )
                ]
            return []

    owner = await UserRepository(session).get_or_create_by_telegram_id(5_032)
    service = _service(session, search=Search())
    await service.search(owner.id, "stars", limit=10, page=1)

    report = await service.search_fresh(
        owner.id,
        "stars",
        page_limit=3,
        results_per_page=10,
    )

    assert report.new_ids == ()
    assert report.pages_checked == 2
    assert report.duplicates == 1
    assert report.exhausted is True


@pytest.mark.asyncio
async def test_two_events_from_one_catalog_url_are_distinct_candidates(
    session: AsyncSession,
) -> None:
    catalog_url = "https://society.example/future-meetings"

    class Search:
        async def search(self, _query: str, *, page: int = 1):
            return [
                SearchResult(title="IAU Symposium 421", url=catalog_url),
                SearchResult(title="IAU Symposium 422", url=catalog_url),
            ]

    owner = await UserRepository(session).get_or_create_by_telegram_id(5_033)
    service = _service(session, search=Search())

    report = await service.search_fresh(
        owner.id,
        "IAU symposia",
        page_limit=1,
        results_per_page=10,
    )

    candidates = await service.repository.list_candidates(owner.id)
    assert len(report.new_ids) == 2
    assert [item.title for item in candidates] == [
        "IAU Symposium 421",
        "IAU Symposium 422",
    ]


@pytest.mark.asyncio
async def test_inspect_does_not_track_but_add_returns_tracked_receipt(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(501)
    fetcher, ai = FakeFetcher(), FakeAI()
    service = _service(session, fetcher=fetcher, ai=ai)
    candidate = (await service.search(owner.id, "IMBASE", limit=1))[0]

    inspected = await service.inspect(owner.id, candidate.id)

    assert inspected.name == "ImBaSE 2027"
    assert await session.scalar(select(func.count()).select_from(Conference)) == 0
    assert await session.scalar(select(func.count()).select_from(Subscription)) == 0
    refreshed = await session.get(EventCandidate, candidate.id)
    assert refreshed.verification_status.value == "SECONDARY_UNVERIFIED"
    assert fetcher.urls == [candidate.source_url]
    assert ai.extracted_urls == [candidate.source_url]

    receipt = await service.add(owner.id, candidate.id)

    assert receipt.committed is True
    assert receipt.tracked is True
    assert receipt.subscription_id is not None
    assert (await session.get(EventCandidate, candidate.id)).status.value == "IMPORTED"
    assert fetcher.urls == [candidate.source_url]
    assert ai.extracted_urls == [candidate.source_url]


@pytest.mark.asyncio
async def test_add_reuses_snapshot_and_persists_exact_and_approximate_deadlines(
    session: AsyncSession,
) -> None:
    class DeadlineAI(FakeAI):
        async def extract_conference_research(self, source_url, text, expected_name):
            base = await super().extract_conference_research(source_url, text, expected_name)
            payload = base.model_dump(mode="json")
            payload["deadlines"] = [
                        {
                            "deadline_type": "ABSTRACT_SUBMISSION",
                            "exact_date": "2027-01-15",
                            "source_url": source_url,
                            "source_class": "SECONDARY",
                            "excerpt": "Abstract deadline: 2027-01-15",
                            "verification_status": "SECONDARY_UNVERIFIED",
                        },
                        {
                            "deadline_type": "REGISTRATION",
                            "approximate_year": 2027,
                            "approximate_month": 5,
                            "tentative": True,
                            "source_url": source_url,
                            "source_class": "SECONDARY",
                            "excerpt": "Registration deadline: May 2027",
                            "verification_status": "TENTATIVE",
                        },
                    ]
            return ConferenceResearch.model_validate(payload)

    class DeadlineFetcher(FakeFetcher):
        async def fetch(self, url: str, *, max_bytes: int):
            self.urls.append(url)
            return FetchedPage(
                url,
                "<p>July 12-16, 2027</p>"
                "<p>Abstract deadline: 2027-01-15</p>"
                "<p>Registration deadline: May 2027</p>",
            )

    owner = await UserRepository(session).get_or_create_by_telegram_id(512)
    fetcher, ai = DeadlineFetcher(), DeadlineAI()
    service = _service(session, fetcher=fetcher, ai=ai)
    candidate = (await service.search(owner.id, "IMBASE", limit=1))[0]

    await service.inspect(owner.id, candidate.id)
    receipt = await service.add(owner.id, candidate.id)

    deadlines = await ReminderRepository(session).upcoming(owner.id, date(2026, 10, 19))
    assert fetcher.urls == [candidate.source_url]
    assert ai.extracted_urls == [candidate.source_url]
    assert len(receipt.deadlines) == 2
    assert any(item.deadline_date == date(2027, 1, 15) for item in deadlines)
    assert any(
        (item.approximate_year, item.approximate_month) == (2027, 5)
        for item in receipt.deadlines
    )


@pytest.mark.asyncio
async def test_failed_snapshot_import_keeps_candidate_active_and_rolls_back(
    session: AsyncSession, monkeypatch
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(513)
    service = _service(session)
    candidate = (await service.search(owner.id, "IMBASE", limit=1))[0]
    await service.inspect(owner.id, candidate.id)

    async def fail_persist(*_args, **_kwargs):
        raise RuntimeError("forced persistence failure")

    monkeypatch.setattr(
        "conference_scout.conferences.research.ConferencePersistenceService.persist",
        fail_persist,
    )

    with pytest.raises(RuntimeError, match="forced persistence failure"):
        await service.add(owner.id, candidate.id)

    await session.refresh(candidate)
    assert candidate.status.value == "ACTIVE"
    assert await session.scalar(select(func.count()).select_from(Conference)) == 0
    assert await session.scalar(select(func.count()).select_from(Subscription)) == 0


@pytest.mark.asyncio
async def test_repeated_search_does_not_downgrade_verified_candidate(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(504)
    service = _service(session)
    candidate = (await service.search(owner.id, "IMBASE", limit=1))[0]
    await service.inspect(owner.id, candidate.id)

    repeated = (await service.search(owner.id, "IMBASE", limit=1))[0]

    assert repeated.id == candidate.id
    assert repeated.title == "ImBaSE 2027"
    assert repeated.verification_status.value == "SECONDARY_UNVERIFIED"


@pytest.mark.asyncio
async def test_skip_and_cross_owner_access_are_rejected(session: AsyncSession) -> None:
    first = await UserRepository(session).get_or_create_by_telegram_id(501)
    second = await UserRepository(session).get_or_create_by_telegram_id(502)
    service = _service(session)
    candidate = (await service.search(first.id, "IMBASE", limit=1))[0]

    deferred = await service.defer(first.id, candidate.id)

    assert deferred.status == CandidateStatus.SKIPPED
    assert deferred.decision_reason == CandidateDecisionReason.USER_DEFERRED

    with pytest.raises(CandidateSkippedError, match="candidate skipped"):
        await service.inspect(first.id, candidate.id)
    with pytest.raises(ValueError, match="candidate not found"):
        await service.inspect(second.id, candidate.id)

    restored = await service.restore(first.id, candidate.id)
    assert restored.status == CandidateStatus.ACTIVE

    rejected = await service.reject(first.id, candidate.id)
    assert rejected.status == CandidateStatus.REJECTED
    assert rejected.decision_reason == CandidateDecisionReason.USER_REJECTED
    with pytest.raises(ValueError, match="candidate rejected"):
        await service.inspect(first.id, candidate.id)


@pytest.mark.asyncio
async def test_prepare_for_tracking_reuses_fresh_snapshot(session: AsyncSession) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(514)
    fetcher = FakeFetcher()
    service = _service(session, fetcher=fetcher)
    candidate = (await service.search(owner.id, "IMBASE", limit=1))[0]

    await service.inspect(owner.id, candidate.id)
    prepared = await service.prepare_for_tracking(owner.id, candidate.id)

    assert prepared.id == candidate.id
    assert prepared.research_payload is not None
    assert fetcher.urls == [candidate.source_url]


@pytest.mark.asyncio
async def test_prepare_for_tracking_verifies_missing_snapshot(session: AsyncSession) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(515)
    fetcher = FakeFetcher()
    service = _service(session, fetcher=fetcher)
    candidate = (await service.search(owner.id, "IMBASE", limit=1))[0]

    prepared = await service.prepare_for_tracking(owner.id, candidate.id)

    assert prepared.research_payload is not None
    assert fetcher.urls == [candidate.source_url]


@pytest.mark.asyncio
async def test_prepare_for_tracking_keeps_unverifiable_candidate_active(
    session: AsyncSession,
) -> None:
    class EmptyFetcher:
        async def fetch(self, url: str, *, max_bytes: int):
            return FetchedPage(url, "<h1>Conference announcement</h1>")

    class EmptyAI(FakeAI):
        async def extract_conference_research(
            self, source_url: str, _text: str, expected_name: str
        ):
            return ConferenceResearch.model_validate(
                {
                    "name": expected_name,
                    "source_url": source_url,
                    "source_class": "SECONDARY",
                    "facts": [],
                    "deadlines": [],
                }
            )

    owner = await UserRepository(session).get_or_create_by_telegram_id(516)
    service = _service(session, fetcher=EmptyFetcher(), ai=EmptyAI())
    candidate = (await service.search(owner.id, "IMBASE", limit=1))[0]

    with pytest.raises(CandidateUnusableError):
        await service.prepare_for_tracking(owner.id, candidate.id)

    await session.refresh(candidate)
    assert candidate.status == CandidateStatus.ACTIVE
    assert candidate.research_payload is None


@pytest.mark.asyncio
async def test_candidate_survives_restart_and_reports_terminal_states(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'candidates.db'}"
    run_migrations(database_url)
    engine, factory = create_engine_and_sessionmaker(database_url)
    try:
        async with factory() as first_session:
            owner = await UserRepository(first_session).get_or_create_by_telegram_id(501)
            candidate = (await _service(first_session).search(owner.id, "IMBASE", limit=1))[0]
            owner_id = owner.id
            candidate_id = candidate.id

        async with factory() as restarted_session:
            restarted = _service(restarted_session)
            inspected = await restarted.inspect(owner_id, candidate_id)
            assert inspected.name == "ImBaSE 2027"
            await restarted.add(owner_id, candidate_id)
            with pytest.raises(CandidateImportedError, match="candidate imported"):
                await restarted.inspect(owner_id, candidate_id)

        async with factory() as expiry_session:
            candidate = await expiry_session.get(EventCandidate, candidate_id)
            assert candidate is not None
            candidate.status = "ACTIVE"
            candidate.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await expiry_session.commit()
            with pytest.raises(CandidateExpiredError, match="candidate expired"):
                await _service(expiry_session).inspect(owner_id, candidate_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inspect_catalog_replaces_generic_parent_with_concrete_candidates(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(601)
    ai = CatalogAI(
        (
            _catalog_event("Звёздная астрономия 2026", "2026-11-03"),
            _catalog_event("Binary Stars 2027", "2027-07-12"),
        )
    )
    service = _service(session, search=CatalogSearch(), ai=ai)
    parent = (await service.search(owner.id, "звёздная астрономия", limit=1))[0]

    expansion = await service.inspect(owner.id, parent.id)

    assert isinstance(expansion, CatalogExpansion)
    assert expansion.parent_candidate_id == parent.id
    assert len(expansion.created_candidate_ids) == 2
    stored_parent = await session.get(EventCandidate, parent.id)
    assert stored_parent is not None and stored_parent.status.value == "SKIPPED"
    active = await service.repository.list_candidates(owner.id)
    assert [item.title for item in active] == [
        "Звёздная астрономия 2026",
        "Binary Stars 2027",
    ]
    assert {item.source_url for item in active} == {parent.source_url}

    repeated = await service.inspect(owner.id, parent.id)

    assert repeated == expansion
    assert await session.scalar(select(func.count()).select_from(EventCandidate)) == 3


@pytest.mark.asyncio
async def test_generic_catalog_assigns_each_event_its_detail_link(
    session: AsyncSession,
) -> None:
    catalog_url = "https://society.example.org/meetings/"
    first_url = "https://society.example.org/meetings/stellar-physics-2027"
    second_url = "https://external-host.example/binary-stars-2027/"

    class Search:
        async def search(self, _query: str, *, page: int = 1):
            return [
                SearchResult(
                    title="Conference calendar",
                    url=catalog_url,
                    snippet="Conference calendar",
                )
            ]

    class Fetcher:
        async def fetch(self, url: str, *, max_bytes: int):
            assert url == catalog_url
            return FetchedPage(
                url,
                f"""<main>
                <a href="/meetings/stellar-physics-2027">Stellar Physics 2027</a>
                <p>3-7 May 2027</p>
                <a href="{second_url}">Binary Stars 2027</a>
                <p>12-16 July 2027</p>
                </main>""",
            )

    ai = CatalogAI(
        (
            _catalog_event("Stellar Physics 2027", "2027-05-03"),
            _catalog_event("Binary Stars 2027", "2027-07-12"),
        )
    )
    owner = await UserRepository(session).get_or_create_by_telegram_id(607)
    service = ConferenceResearchService(
        session,
        Search(),
        Fetcher(),
        ai,
        max_page_bytes=1_000_000,
        max_page_chars=100_000,
    )
    parent = (await service.search(owner.id, "stellar meetings", limit=1))[0]

    expansion = await service.inspect(owner.id, parent.id)

    assert isinstance(expansion, CatalogExpansion)
    children = await service.repository.list_candidates(owner.id)
    assert [(item.title, item.source_url) for item in children] == [
        ("Stellar Physics 2027", first_url),
        ("Binary Stars 2027", second_url),
    ]


@pytest.mark.asyncio
async def test_event_inspection_follows_relevant_deadline_link_and_keeps_its_source(
    session: AsyncSession,
) -> None:
    event_url = "https://conference.example.org/stellar-2027/"
    dates_url = "https://conference.example.org/stellar-2027/important-dates"

    class Search:
        async def search(self, _query: str, *, page: int = 1):
            return [SearchResult(title="Stellar Physics 2027", url=event_url)]

    class Fetcher:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def fetch(self, url: str, *, max_bytes: int):
            self.urls.append(url)
            if url == event_url:
                return FetchedPage(
                    url,
                    '<main><h1>Stellar Physics 2027</h1>'
                    '<p>May 3-7, 2027</p>'
                    '<a href="important-dates">Important dates and registration</a>'
                    '<a href="https://social.example.org/profile">Social network</a></main>',
                )
            assert url == dates_url
            return FetchedPage(
                url,
                "<main><h1>Important dates</h1>"
                "<p>Abstract submission deadline: February 12, 2027</p></main>",
            )

    class RelatedPageAI(FakeAI):
        async def extract_conference_catalog(self, source_url: str, _text: str):
            return ConferenceCatalogResearch(events=())

        async def extract_conference_research(
            self, source_url: str, text: str, expected_name: str
        ):
            assert "DOCUMENT_URL: " + dates_url in text
            return ConferenceResearch.model_validate(
                {
                    "name": expected_name,
                    "source_url": source_url,
                    "source_class": "OFFICIAL_CONFERENCE",
                    "facts": [
                        {
                            "kind": "start_date",
                            "value": "2027-05-03",
                            "source_url": source_url,
                            "source_class": "OFFICIAL_CONFERENCE",
                            "excerpt": "May 3-7, 2027",
                            "verification_status": "OFFICIAL_CONFIRMED",
                        }
                    ],
                    "deadlines": [
                        {
                            "deadline_type": "ABSTRACT_SUBMISSION",
                            "exact_date": "2027-02-12",
                            "source_url": source_url,
                            "source_class": "OFFICIAL_CONFERENCE",
                            "excerpt": "Abstract submission deadline: February 12, 2027",
                            "verification_status": "OFFICIAL_CONFIRMED",
                        }
                    ],
                }
            )

    owner = await UserRepository(session).get_or_create_by_telegram_id(608)
    fetcher = Fetcher()
    service = ConferenceResearchService(
        session,
        Search(),
        fetcher,
        RelatedPageAI(),
        max_page_bytes=1_000_000,
        max_page_chars=100_000,
    )
    candidate = (await service.search(owner.id, "stellar 2027", limit=1))[0]

    inspected = await service.inspect(owner.id, candidate.id)

    assert fetcher.urls == [event_url, dates_url]
    assert str(inspected.deadlines[0].source_url).rstrip("/") == dates_url


@pytest.mark.asyncio
async def test_unavailable_child_page_keeps_primary_facts_and_adds_note(
    session: AsyncSession,
) -> None:
    event_url = "https://conference.example.org/stellar-2028/"
    dates_url = "https://conference.example.org/stellar-2028/important-dates"

    class Search:
        async def search(self, _query: str, *, page: int = 1):
            return [SearchResult(title="Stellar Physics 2028", url=event_url)]

    class Fetcher:
        async def fetch(self, url: str, *, max_bytes: int):
            if url == dates_url:
                raise FetchError("forbidden", code="HTTP_403")
            return FetchedPage(
                url,
                '<p>May 3-7, 2028</p><a href="important-dates">Important dates</a>',
            )

    class AI(FakeAI):
        async def extract_conference_research(self, source_url, _text, expected_name):
            return ConferenceResearch.model_validate(
                {
                    "name": expected_name,
                    "source_url": source_url,
                    "source_class": "OFFICIAL_CONFERENCE",
                    "facts": [
                        {
                            "kind": "start_date",
                            "value": "2028-05-03",
                            "source_url": source_url,
                            "source_class": "OFFICIAL_CONFERENCE",
                            "excerpt": "May 3-7, 2028",
                            "verification_status": "OFFICIAL_CONFIRMED",
                        }
                    ],
                }
            )

    owner = await UserRepository(session).get_or_create_by_telegram_id(614)
    service = _service(session, search=Search(), fetcher=Fetcher(), ai=AI())
    candidate = (await service.search(owner.id, "stellar 2028", limit=1))[0]

    inspected = await service.inspect(owner.id, candidate.id)

    assert any(item.kind == "start_date" for item in inspected.facts)
    assert any("HTTP_403" in note and dates_url in note for note in inspected.verifier_notes)


@pytest.mark.asyncio
async def test_concrete_candidate_on_catalog_follows_its_event_link_and_updates_card(
    session: AsyncSession,
) -> None:
    catalog_url = "https://society.example.org/future-meetings"
    event_url = "https://society.example.org/events/isya-vietnam-2026"
    title = "ISYA Vietnam 2026: International School for Young Astronomers"

    class Search:
        async def search(self, _query: str, *, page: int = 1):
            return [SearchResult(title=title, url=catalog_url)]

    class Fetcher:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def fetch(self, url: str, *, max_bytes: int):
            self.urls.append(url)
            if url == catalog_url:
                return FetchedPage(
                    url,
                    f'<main><a href="{event_url}">{title}</a>'
                    "<p>September 28 - October 16, 2026</p></main>",
                )
            assert url == event_url
            return FetchedPage(
                url,
                "<main><h1>ISYA Vietnam 2026</h1>"
                "<p>September 28 - October 16, 2026, Quy Nhon, Vietnam</p></main>",
            )

    catalog_event = ConferenceResearch.model_validate(
        {
            "name": title,
            "source_url": catalog_url,
            "source_class": "OFFICIAL_ORGANIZER",
            "facts": [
                {
                    "kind": "start_date",
                    "value": "2026-09-28",
                    "source_url": catalog_url,
                    "source_class": "OFFICIAL_ORGANIZER",
                    "excerpt": "September 28 - October 16, 2026",
                    "verification_status": "ORGANIZER_CONFIRMED",
                }
            ],
            "deadlines": [],
        }
    )

    class AI(FakeAI):
        async def extract_conference_catalog(self, source_url: str, _text: str):
            return ConferenceCatalogResearch(
                events=(catalog_event,) if source_url == catalog_url else ()
            )

        async def extract_conference_research(
            self, source_url: str, _text: str, expected_name: str
        ):
            assert source_url == event_url
            payload = catalog_event.model_dump(mode="json")
            payload["source_url"] = event_url
            return ConferenceResearch.model_validate(payload)

    owner = await UserRepository(session).get_or_create_by_telegram_id(609)
    fetcher = Fetcher()
    service = ConferenceResearchService(
        session,
        Search(),
        fetcher,
        AI(),
        max_page_bytes=1_000_000,
        max_page_chars=100_000,
        application_clock=ApplicationClock.start(
            ZoneInfo("UTC"),
            now_provider=lambda: datetime(2026, 9, 1, tzinfo=UTC),
        ),
    )
    candidate = (await service.search(owner.id, "ISYA Vietnam", limit=1))[0]

    inspected = await service.inspect(owner.id, candidate.id)

    assert inspected.name == title
    assert fetcher.urls == [catalog_url, event_url]
    await session.refresh(candidate)
    assert candidate.source_url == event_url


@pytest.mark.asyncio
async def test_catalog_without_event_dates_or_deadlines_stays_active(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(602)
    service = _service(
        session,
        search=CatalogSearch(),
        ai=CatalogAI((_catalog_event("Possible future meeting"),)),
    )
    parent = (await service.search(owner.id, "future astronomy", limit=1))[0]

    with pytest.raises(CandidateUnusableError, match="no concrete events"):
        await service.inspect(owner.id, parent.id)

    await session.refresh(parent)
    assert parent.status.value == "ACTIVE"
    assert await session.scalar(select(func.count()).select_from(EventCandidate)) == 1


@pytest.mark.asyncio
async def test_inspect_uses_search_snippet_when_catalog_page_omits_event_block(
    session: AsyncSession,
) -> None:
    summary = "December 6-11, 2027, Goa, India"
    source_url = "https://www.iau.org/science/scientific-meetings/iaus421/"
    event = ConferenceResearch.model_validate(
        {
            "name": "IAU Symposium 421 Dusty Stellar Environments",
            "source_url": source_url,
            "source_class": "OFFICIAL_CONFERENCE",
            "facts": [
                {
                    "kind": "start_date",
                    "value": "2027-12-06",
                    "source_url": source_url,
                    "source_class": "OFFICIAL_CONFERENCE",
                    "excerpt": summary,
                    "verification_status": "OFFICIAL_CONFIRMED",
                },
                {
                    "kind": "end_date",
                    "value": "2027-12-11",
                    "source_url": source_url,
                    "source_class": "OFFICIAL_CONFERENCE",
                    "excerpt": summary,
                    "verification_status": "OFFICIAL_CONFIRMED",
                },
            ],
            "deadlines": [],
        }
    )

    class Search:
        async def search(self, _query: str, *, page: int = 1):
            return [
                SearchResult(
                    title="IAU Symposium 421 Dusty Stellar Environments",
                    url=source_url,
                    snippet=summary,
                )
            ]

    class SummaryAwareAI(FakeAI):
        def __init__(self) -> None:
            super().__init__()
            self.catalog_texts: list[str] = []

        async def extract_conference_catalog(self, source_url: str, text: str):
            self.catalog_texts.append(text)
            events = (event,) if summary in text else ()
            return ConferenceCatalogResearch(events=events)

    owner = await UserRepository(session).get_or_create_by_telegram_id(605)
    ai = SummaryAwareAI()
    service = _service(session, search=Search(), ai=ai)
    candidate = (await service.search(owner.id, "IAUS 421", limit=1))[0]

    inspected = await service.inspect(owner.id, candidate.id)

    assert inspected.name == event.name
    assert [fact.value for fact in inspected.facts] == ["2027-12-06", "2027-12-11"]
    assert all(fact.source_class.value == "SECONDARY" for fact in inspected.facts)
    assert all(
        fact.verification_status.value == "SECONDARY_UNVERIFIED"
        for fact in inspected.facts
    )
    assert summary in ai.catalog_texts[0]


@pytest.mark.asyncio
async def test_inspect_recovers_visible_event_date_range_when_model_omits_it(
    session: AsyncSession,
) -> None:
    source_url = "https://www.iau.org/IAU/Iau/Science/Scientific-Meetings/Future-Meetings.aspx"
    title = (
        "IAU Symposium: IAUS 421 Dusty Stellar Environments Shaped by Winds, "
        "Eruptions, and Explosions"
    )

    class Search:
        async def search(self, _query: str, *, page: int = 1):
            return [SearchResult(title=title, url=source_url, snippet="IAU meeting")]

    class Fetcher:
        async def fetch(self, url: str, *, max_bytes: int):
            return FetchedPage(
                url,
                f"<main><h4>{title}</h4><p>December 6 - 11, 2027, Goa, India</p></main>",
            )

    class DateOmittingAI(FakeAI):
        async def extract_conference_catalog(self, source_url: str, _text: str):
            return ConferenceCatalogResearch(events=())

        async def extract_conference_research(
            self, source_url: str, _text: str, expected_name: str
        ):
            return ConferenceResearch.model_validate(
                {
                    "name": expected_name,
                    "source_url": source_url,
                    "source_class": "OFFICIAL_ORGANIZER",
                    "facts": [],
                    "deadlines": [],
                }
            )

    owner = await UserRepository(session).get_or_create_by_telegram_id(606)
    service = ConferenceResearchService(
        session,
        Search(),
        Fetcher(),
        DateOmittingAI(),
        max_page_bytes=1_000_000,
        max_page_chars=100_000,
    )
    candidate = (await service.search(owner.id, "IAUS 421", limit=1))[0]

    inspected = await service.inspect(owner.id, candidate.id)

    assert [
        (fact.kind, fact.value, fact.excerpt, fact.verification_status.value)
        for fact in inspected.facts
    ] == [
        (
            "start_date",
            "2027-12-06",
            "December 6 - 11, 2027, Goa, India",
            "ORGANIZER_CONFIRMED",
        ),
        (
            "end_date",
            "2027-12-11",
            "December 6 - 11, 2027, Goa, India",
            "ORGANIZER_CONFIRMED",
        ),
    ]


@pytest.mark.asyncio
async def test_add_rejects_generic_or_evidence_empty_candidate_without_importing(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(603)
    service = _service(
        session,
        search=CatalogSearch(),
        ai=CatalogAI((_catalog_event("Possible future meeting"),)),
    )
    candidate = (await service.search(owner.id, "future astronomy", limit=1))[0]

    with pytest.raises(CandidateUnusableError):
        await service.add(owner.id, candidate.id)

    await session.refresh(candidate)
    assert candidate.status.value == "ACTIVE"
    assert await session.scalar(select(func.count()).select_from(Conference)) == 0
    assert await session.scalar(select(func.count()).select_from(Subscription)) == 0


@pytest.mark.asyncio
async def test_catalog_event_can_be_added_and_appears_in_upcoming_deadlines(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(604)
    source_url = "https://www.sai.msu.ru/conference/annconf/index.html"
    event = ConferenceResearch.model_validate(
        {
            "name": "Звёздная астрономия 2027",
            "source_url": source_url,
            "source_class": "SECONDARY",
            "facts": [
                {
                    "kind": "start_date",
                    "value": "2027-11-03",
                    "source_url": source_url,
                    "source_class": "SECONDARY",
                    "excerpt": "состоится 3-5 ноября 2027 г.",
                    "verification_status": "SECONDARY_UNVERIFIED",
                }
            ],
            "deadlines": [
                {
                    "deadline_type": "ABSTRACT_SUBMISSION",
                    "exact_date": "2027-09-15",
                    "source_url": source_url,
                    "source_class": "SECONDARY",
                    "excerpt": "Тезисы принимаются до 15 сентября 2027 г.",
                    "verification_status": "SECONDARY_UNVERIFIED",
                },
                {
                    "deadline_type": "REGISTRATION",
                    "approximate_year": 2027,
                    "approximate_month": 10,
                    "tentative": True,
                    "source_url": source_url,
                    "source_class": "SECONDARY",
                    "excerpt": "Регистрация завершается в октябре 2027 г. (предварительно)",
                    "verification_status": "TENTATIVE",
                },
            ],
        }
    )
    class EvidenceFetcher(FakeFetcher):
        async def fetch(self, url: str, *, max_bytes: int):
            self.urls.append(url)
            return FetchedPage(
                url,
                "<main><h1>Звёздная астрономия 2027</h1>"
                "<p>состоится 3-5 ноября 2027 г.</p>"
                "<p>Тезисы принимаются до 15 сентября 2027 г.</p>"
                "<p>Регистрация завершается в октябре 2027 г. (предварительно)</p>"
                "</main>",
            )

    service = _service(
        session,
        search=CatalogSearch(),
        fetcher=EvidenceFetcher(),
        ai=CatalogAI((event,)),
    )
    parent = (await service.search(owner.id, "звёздная астрономия 2027", limit=1))[0]
    expansion = await service.inspect(owner.id, parent.id)
    assert isinstance(expansion, CatalogExpansion)
    child_id = expansion.created_candidate_ids[0]

    receipt = await service.add(owner.id, child_id)
    upcoming = await ReminderRepository(session).upcoming(owner.id, date(2027, 1, 1))

    assert receipt.tracked is True
    assert [item.conference_name for item in upcoming] == [
        "Звёздная астрономия 2027",
        "Звёздная астрономия 2027",
    ]
    assert upcoming[0].deadline_date == date(2027, 9, 15)
    assert (upcoming[1].approximate_year, upcoming[1].approximate_month) == (2027, 10)
    assert await session.scalar(select(func.count()).select_from(Conference)) == 1
