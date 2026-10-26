import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from typing import ClassVar, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.conferences.candidate_policy import (
    TopicRelevanceContext,
    evaluate_keyword_relevance,
    is_expired_research,
)
from conference_planner.conferences.deduplication import canonical_url, normalize_name
from conference_planner.conferences.evidence import (
    EvidenceDocument,
    EvidenceValidator,
    verification_required,
)
from conference_planner.conferences.persistence import (
    ConferencePersistenceService,
    PersistenceReceipt,
)
from conference_planner.conferences.roles import ResearchRole, VerifierRole
from conference_planner.conferences.temporal import enrich_catalog_events
from conference_planner.db.models import EventCandidate
from conference_planner.db.repositories.conference_research import (
    CandidateDraft,
    ConferenceResearchRepository,
)
from conference_planner.db.repositories.topics import TopicRepository
from conference_planner.db.repositories.web_cache import WebDocumentCacheRepository
from conference_planner.runtime_clock import ApplicationClock
from conference_planner.schemas.common import (
    CandidateDecisionReason,
    CandidateDecisionSource,
    CandidateStatus,
    EvidenceSourceClass,
    VerificationStatus,
)
from conference_planner.schemas.research import (
    ConferenceCatalogResearch,
    ConferenceResearch,
    FactClaim,
    VerifiedConferenceResearch,
)
from conference_planner.schemas.topics import RelevanceAssessment
from conference_planner.search.provider import SearchProvider
from conference_planner.web.fetcher import FetchedPage, FetchError, normalized_content_hash
from conference_planner.web.parser import PageLink, extract_page_links, extract_page_text


class CandidateExpiredError(ValueError):
    pass


class CandidateSkippedError(ValueError):
    pass


class CandidateRejectedError(ValueError):
    pass


class CandidateImportedError(ValueError):
    pass


class CandidateUnusableError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogExpansion:
    parent_candidate_id: int
    created_candidate_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CandidateSearchReport:
    active_ids: tuple[int, ...]
    new_ids: tuple[int, ...]
    pages_checked: int
    duplicates: int
    exhausted: bool


class Fetcher(Protocol):
    async def fetch(self, url: str, *, max_bytes: int) -> FetchedPage: ...


class ResearchAnalyzer(Protocol):
    async def extract_conference_catalog(
        self, source_url: str, text: str
    ) -> ConferenceCatalogResearch: ...

    async def extract_conference_research(
        self, source_url: str, text: str, expected_name: str
    ) -> ConferenceResearch: ...

    async def verify_conference_research(
        self, research: ConferenceResearch
    ) -> VerifiedConferenceResearch: ...

    async def rank_research_relevance(
        self,
        research: VerifiedConferenceResearch,
        topics: tuple[TopicRelevanceContext, ...],
    ) -> RelevanceAssessment: ...


class ConferenceResearchService:
    MAX_PAGE_RESULTS = 50

    def __init__(
        self,
        session: AsyncSession,
        search: SearchProvider,
        fetcher: Fetcher,
        ai: ResearchAnalyzer,
        *,
        max_page_bytes: int,
        max_page_chars: int,
        candidate_ttl: timedelta = timedelta(days=7),
        application_clock: ApplicationClock | None = None,
        min_relevance_score: float = 0.5,
        cache: WebDocumentCacheRepository | None = None,
    ) -> None:
        self.session = session
        self.search_provider = search
        self.fetcher = fetcher
        self.ai = ai
        self.max_page_bytes = max_page_bytes
        self.max_page_chars = max_page_chars
        self.candidate_ttl = candidate_ttl
        self.application_clock = application_clock
        self.min_relevance_score = min_relevance_score
        self.repository = ConferenceResearchRepository(session)
        self.cache = cache or WebDocumentCacheRepository(session)
        self.last_search_new_count = 0
        self.last_search_new_ids: tuple[int, ...] = ()
        self.last_search_result_count = 0
        self.last_search_duplicate_count = 0

    async def search(
        self, user_id: int, query: str, limit: int = 10, *, page: int = 1
    ) -> list[EventCandidate]:
        bounded_limit = max(1, min(limit, self.MAX_PAGE_RESULTS))
        topics = await self._topic_contexts(user_id)
        search_query = query
        if self.application_clock is not None:
            search_query = (
                f"{query}. Текущая дата: {self.application_clock.today().isoformat()}. "
                "Искать только ещё не завершившиеся научные мероприятия."
            )
        raw_results = (await self.search_provider.search(search_query, page=page))[:bounded_limit]
        self.last_search_result_count = len(raw_results)
        drafts: list[CandidateDraft] = []
        seen_identities: set[tuple[str, str]] = set()
        for position, result in enumerate(raw_results):
            url = str(result.url)
            normalized_url = canonical_url(url) or url
            identity = (normalized_url, normalize_name(result.title))
            if identity in seen_identities:
                continue
            keyword_relevance = evaluate_keyword_relevance(result.title, result.snippet, topics)
            if keyword_relevance.explicitly_irrelevant:
                continue
            seen_identities.add(identity)
            drafts.append(
                CandidateDraft(
                    title=result.title,
                    source_url=url,
                    query=query,
                    relevance_score=(
                        keyword_relevance.score if topics else max(0.0, 1.0 - position * 0.02)
                    ),
                    summary=result.snippet,
                    verification_status=VerificationStatus.UNVERIFIABLE,
                )
            )
        existing_identities = list(
            (
                await self.session.execute(
                    select(EventCandidate.source_url, EventCandidate.normalized_title).where(
                        EventCandidate.user_id == user_id,
                        EventCandidate.source_url.in_([item.source_url for item in drafts]),
                    )
                )
            ).tuples()
        )
        new_draft_identities = {
            self._candidate_identity(item)
            for item in drafts
            if not self._candidate_identity_exists(item, existing_identities)
        }
        stored = await self.repository.create_candidates(
            user_id,
            drafts,
            expires_at=datetime.now(UTC) + self.candidate_ttl,
        )
        await self.session.commit()
        self.last_search_new_ids = tuple(
            item.id
            for item in stored
            if item.status == CandidateStatus.ACTIVE
            and self._stored_candidate_identity(item) in new_draft_identities
        )
        self.last_search_new_count = len(self.last_search_new_ids)
        self.last_search_duplicate_count = max(
            0,
            len(drafts) - self.last_search_new_count,
        )
        return [item for item in stored if item.status == CandidateStatus.ACTIVE]

    async def search_fresh(
        self,
        user_id: int,
        query: str,
        *,
        page_limit: int,
        results_per_page: int,
    ) -> CandidateSearchReport:
        bounded_pages = max(1, min(page_limit, 100))
        bounded_results = max(1, min(results_per_page, self.MAX_PAGE_RESULTS))
        active_ids: list[int] = []
        new_ids: list[int] = []
        duplicates = 0
        exhausted = False
        pages_checked = 0

        for page in range(1, bounded_pages + 1):
            candidates = await self.search(
                user_id,
                query,
                limit=bounded_results,
                page=page,
            )
            pages_checked += 1
            for candidate in candidates:
                if candidate.id not in active_ids:
                    active_ids.append(candidate.id)
            for candidate_id in self.last_search_new_ids:
                if candidate_id not in new_ids:
                    new_ids.append(candidate_id)
            duplicates += self.last_search_duplicate_count

            if new_ids:
                break
            if self.last_search_result_count == 0:
                exhausted = True
                break

        return CandidateSearchReport(
            active_ids=tuple(active_ids),
            new_ids=tuple(new_ids),
            pages_checked=pages_checked,
            duplicates=duplicates,
            exhausted=exhausted,
        )

    async def inspect(
        self,
        user_id: int,
        candidate_id: int,
        *,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> VerifiedConferenceResearch | CatalogExpansion:
        candidate = await self.repository.get_candidate(user_id, candidate_id)
        if candidate.status == CandidateStatus.SKIPPED and self._is_generic_title(candidate.title):
            siblings = [
                item
                for item in await self.repository.list_candidates(user_id, limit=100)
                if item.source_url == candidate.source_url and item.id != candidate.id
            ]
            if siblings:
                return CatalogExpansion(candidate.id, tuple(item.id for item in siblings))
        candidate = await self._require_active(user_id, candidate_id)
        await self._report(progress, f"Открываю сайт: {candidate.source_url}")
        page, page_text, page_links = await self._fetch_candidate_page(candidate)
        catalog_text = self._candidate_evidence_text(candidate, page_text)
        catalog = await self._extract_catalog(page.url, catalog_text)
        if catalog is not None:
            catalog = enrich_catalog_events(catalog, page.url, page_text, page_links)
            catalog = self._downgrade_catalog_snippet_evidence(catalog, candidate.summary)
        if catalog is not None and self._should_expand_catalog(candidate, catalog):
            return await self._expand_catalog(candidate, catalog, page_links)
        detail_link = (
            self._best_event_link(candidate.title, page_links)
            if catalog is not None and catalog.events
            else None
        )
        if detail_link is not None and detail_link.url.rstrip("/") != page.url.rstrip("/"):
            detail_page = await self.fetcher.fetch(
                detail_link.url,
                max_bytes=self.max_page_bytes,
            )
            page = detail_page
            page_text = extract_page_text(page.html, max_chars=self.max_page_chars)
            page_links = extract_page_links(page.html, page.url)
            duplicate = await self.session.scalar(
                select(EventCandidate.id).where(
                    EventCandidate.user_id == candidate.user_id,
                    EventCandidate.source_url == page.url,
                    EventCandidate.normalized_title == candidate.normalized_title,
                    EventCandidate.id != candidate.id,
                )
            )
            if duplicate is None:
                candidate.source_url = page.url
                await self.session.commit()
            catalog = None
        return await self._research_candidate(
            candidate,
            page=page,
            page_text=page_text,
            page_links=page_links,
            catalog=catalog,
            progress=progress,
        )

    async def prepare_for_display(
        self,
        user_id: int,
        candidate_id: int,
        *,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> VerifiedConferenceResearch | CatalogExpansion:
        candidate = await self._require_active(user_id, candidate_id)
        snapshot = await self.repository.load_candidate_snapshot(user_id, candidate_id)
        is_fresh = (
            snapshot is not None
            and candidate.researched_at is not None
            and self._now() - candidate.researched_at <= self.candidate_ttl
        )
        if is_fresh:
            return snapshot
        return await self.inspect(user_id, candidate_id, progress=progress)

    async def get_candidate(self, user_id: int, candidate_id: int) -> EventCandidate:
        return await self._require_active(user_id, candidate_id)

    async def add(self, user_id: int, candidate_id: int) -> PersistenceReceipt:
        candidate = await self._require_active(user_id, candidate_id)
        verified = await self.repository.load_candidate_snapshot(user_id, candidate_id)
        snapshot_is_fresh = (
            verified is not None
            and candidate.researched_at is not None
            and self._now() - candidate.researched_at <= self.candidate_ttl
        )
        if not snapshot_is_fresh:
            verified = await self._research_candidate(candidate)
        if is_expired_research(verified, self._today()):
            await self.repository.transition_candidate(
                user_id,
                candidate_id,
                CandidateStatus.EXPIRED,
                CandidateDecisionReason.EVENT_ENDED,
                CandidateDecisionSource.SYSTEM,
                self._now(),
            )
            await self.session.commit()
            raise CandidateExpiredError("event already ended")
        if not self._is_usable_event(verified):
            raise CandidateUnusableError("candidate has no event date or deadline evidence")
        receipt = await ConferencePersistenceService(self.session).persist(
            user_id, verified, track=True
        )
        if not receipt.committed:
            raise RuntimeError("conference persistence did not commit")
        await self.repository.transition_candidate(
            user_id,
            candidate_id,
            CandidateStatus.IMPORTED,
            None,
            CandidateDecisionSource.SYSTEM,
            self._now(),
        )
        await self.session.commit()
        return receipt

    async def prepare_for_tracking(self, user_id: int, candidate_id: int) -> EventCandidate:
        research = await self.prepare_for_display(user_id, candidate_id)
        if isinstance(research, CatalogExpansion):
            raise CandidateUnusableError("catalog expanded into concrete candidates")
        candidate = await self.repository.get_candidate(user_id, candidate_id)
        return candidate

    async def defer(self, user_id: int, candidate_id: int) -> EventCandidate:
        await self._require_active(user_id, candidate_id)
        candidate = await self.repository.transition_candidate(
            user_id,
            candidate_id,
            CandidateStatus.SKIPPED,
            CandidateDecisionReason.USER_DEFERRED,
            CandidateDecisionSource.USER,
            self._now(),
            next_review_at=self._now() + timedelta(days=7),
        )
        await self.session.commit()
        return candidate

    async def reject(self, user_id: int, candidate_id: int) -> EventCandidate:
        await self._require_active(user_id, candidate_id)
        candidate = await self.repository.transition_candidate(
            user_id,
            candidate_id,
            CandidateStatus.REJECTED,
            CandidateDecisionReason.USER_REJECTED,
            CandidateDecisionSource.USER,
            self._now(),
        )
        await self.session.commit()
        return candidate

    async def restore(self, user_id: int, candidate_id: int) -> EventCandidate:
        candidate = await self.repository.get_candidate(user_id, candidate_id)
        if candidate.status not in {CandidateStatus.SKIPPED, CandidateStatus.REJECTED}:
            raise ValueError("candidate is not deferred or rejected")
        candidate = await self.repository.transition_candidate(
            user_id,
            candidate_id,
            CandidateStatus.ACTIVE,
            None,
            CandidateDecisionSource.USER,
            self._now(),
        )
        await self.session.commit()
        return candidate

    async def recheck_skipped(self, user_id: int, candidate_id: int) -> bool:
        candidate = await self.repository.get_candidate(user_id, candidate_id)
        if candidate.status != CandidateStatus.SKIPPED or candidate.decision_reason not in {
            CandidateDecisionReason.USER_DEFERRED,
            CandidateDecisionReason.LOW_RELEVANCE,
        }:
            return False

        cached = await self.cache.get(candidate.source_url)
        page = await self.fetcher.fetch(
            candidate.source_url,
            max_bytes=self.max_page_bytes,
            etag=cached.etag if cached is not None else None,
            last_modified=cached.last_modified if cached is not None else None,
        )
        if page.not_modified:
            if cached is not None:
                await self.cache.touch(
                    cached,
                    etag=page.etag,
                    last_modified=page.last_modified,
                )
                await self.session.commit()
            return False

        content_hash = page.content_hash or normalized_content_hash(page.html)
        if cached is not None and cached.content_hash == content_hash:
            await self.cache.touch(
                cached,
                etag=page.etag,
                last_modified=page.last_modified,
                status_code=page.status_code,
            )
            await self.session.commit()
            return False

        page = FetchedPage(
            page.url,
            page.html,
            status_code=page.status_code,
            etag=page.etag,
            last_modified=page.last_modified,
            content_hash=content_hash,
            not_modified=page.not_modified,
        )
        page_text = extract_page_text(page.html, max_chars=self.max_page_chars)
        page_links = extract_page_links(page.html, page.url)
        previous = await self.repository.load_candidate_snapshot(user_id, candidate_id)
        await self.cache.store(page, page_text, observed_at=self._now())
        verified = await self._research_candidate(
            candidate,
            page=page,
            page_text=page_text,
            page_links=page_links,
        )
        if self._research_signature(verified) == self._research_signature(previous):
            await self.session.commit()
            return False
        if not self._research_signature(verified):
            await self.session.commit()
            return False
        await self.repository.transition_candidate(
            user_id,
            candidate_id,
            CandidateStatus.ACTIVE,
            None,
            CandidateDecisionSource.SYSTEM,
            self._now(),
        )
        await self.session.commit()
        return True

    async def skip(self, user_id: int, candidate_id: int) -> EventCandidate:
        return await self.defer(user_id, candidate_id)

    async def _require_active(self, user_id: int, candidate_id: int) -> EventCandidate:
        candidate = await self.repository.get_candidate(user_id, candidate_id)
        if candidate.status == CandidateStatus.SKIPPED:
            raise CandidateSkippedError("candidate skipped")
        if candidate.status == CandidateStatus.REJECTED:
            raise CandidateRejectedError("candidate rejected")
        if candidate.status == CandidateStatus.IMPORTED:
            raise CandidateImportedError("candidate imported")
        if candidate.status == CandidateStatus.EXPIRED or candidate.expires_at <= datetime.now(UTC):
            await self.repository.transition_candidate(
                user_id,
                candidate_id,
                CandidateStatus.EXPIRED,
                CandidateDecisionReason.CANDIDATE_TTL_EXPIRED,
                CandidateDecisionSource.SYSTEM,
                self._now(),
            )
            await self.session.commit()
            raise CandidateExpiredError("candidate expired")
        return candidate

    async def _research_candidate(
        self,
        candidate: EventCandidate,
        *,
        page: FetchedPage | None = None,
        page_text: str | None = None,
        page_links: tuple[PageLink, ...] | None = None,
        catalog: ConferenceCatalogResearch | None = None,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> VerifiedConferenceResearch:
        if page is None or page_text is None or page_links is None:
            page, page_text, page_links = await self._fetch_candidate_page(candidate)
        if catalog is None:
            catalog = await self._extract_catalog(
                page.url,
                self._candidate_evidence_text(candidate, page_text),
            )
            if catalog is not None:
                catalog = enrich_catalog_events(catalog, page.url, page_text, page_links)
                catalog = self._downgrade_catalog_snippet_evidence(catalog, candidate.summary)
        documents, fetch_notes = await self._fetch_research_documents(
            candidate,
            page,
            page_text,
            page_links,
            progress=progress,
        )
        await self._report(progress, "Модель извлекает сведения о конференции.")
        text = self._candidate_evidence_text(
            candidate,
            self._documents_text(documents),
        )
        if catalog is not None and catalog.events:
            extracted, score = self._best_catalog_event(candidate.title, catalog.events)
            if self._is_generic_title(candidate.title) or (
                len(catalog.events) > 1 and score < 0.45
            ):
                raise CandidateUnusableError("catalog candidate must be inspected first")
            if len(documents) > 1:
                detailed = await self.ai.extract_conference_research(
                    page.url, text, candidate.title
                )
                extracted = self._merge_research(extracted, detailed)
        else:
            extracted = await ResearchRole(self.ai).extract(
                (EvidenceDocument(page.url, text),), candidate.title
            )
        extracted = self._attribute_claim_sources(extracted, documents)
        validator = EvidenceValidator()
        needs_verification = any(
            verification_required(claim, validator.context_for(claim, documents))
            for claim in (*extracted.facts, *extracted.deadlines)
        )
        if needs_verification:
            verified = await VerifierRole(self.ai).verify(extracted, documents)
        else:
            verified = VerifiedConferenceResearch.model_validate(
                extracted.model_dump(mode="python")
            )
        verified = self._recover_visible_event_dates(
            verified,
            text,
            candidate.title,
            page.url,
        )
        verified = self._attribute_claim_sources(verified, documents)
        if fetch_notes:
            verified = verified.model_copy(
                update={"verifier_notes": (*verified.verifier_notes, *fetch_notes)}
            )
        verified = self._downgrade_snippet_evidence(verified, candidate.summary)
        validation = validator.validate(verified, documents)
        rejected = tuple(
            f"Доказательство отклонено: {item.claim_type} {item.claim_index} ({item.reason})"
            for item in validation.results
            if item.state.value in {"REJECTED", "CONFLICT"}
        )
        verified = validation.validated.model_copy(
            update={"verifier_notes": (*validation.validated.verifier_notes, *rejected)[:20]}
        )
        if not self._is_usable_event(verified):
            raise CandidateUnusableError("candidate has no event date or deadline evidence")
        if is_expired_research(verified, self._today()):
            await self.repository.transition_candidate(
                candidate.user_id,
                candidate.id,
                CandidateStatus.EXPIRED,
                CandidateDecisionReason.EVENT_ENDED,
                CandidateDecisionSource.SYSTEM,
                self._now(),
            )
            await self.session.commit()
            raise CandidateExpiredError("event already ended")
        topics = await self._topic_contexts(candidate.user_id)
        relevance = (
            await self.ai.rank_research_relevance(verified, topics)
            if topics
            else RelevanceAssessment(score=1.0, explanation="Темы не настроены")
        )
        candidate.topic_relevance_score = relevance.score
        candidate.topic_relevance_reason = relevance.explanation
        if relevance.score < self.min_relevance_score:
            await self.repository.transition_candidate(
                candidate.user_id,
                candidate.id,
                CandidateStatus.SKIPPED,
                CandidateDecisionReason.LOW_RELEVANCE,
                CandidateDecisionSource.SYSTEM,
                self._now(),
                next_review_at=self._now() + timedelta(days=7),
            )
            await self.session.commit()
            raise CandidateUnusableError("candidate is not relevant to enabled topics")
        candidate.title = verified.name
        candidate.normalized_title = normalize_name(verified.name)
        candidate.verification_status = self._candidate_verification_status(verified)
        researched_at = (
            self.application_clock.now()
            if self.application_clock is not None
            else datetime.now(UTC)
        )
        await self.repository.save_candidate_snapshot(
            candidate.user_id,
            candidate.id,
            verified,
            relevance,
            researched_at,
        )
        await self.session.commit()
        await self._report(progress, "Проверенные сведения сохранены.")
        return verified

    async def _topic_contexts(self, user_id: int) -> tuple[TopicRelevanceContext, ...]:
        topics = await TopicRepository(self.session).list_for_user(user_id)
        return tuple(
            TopicRelevanceContext(
                name=topic.name,
                description=topic.description,
                keywords=tuple(topic.keywords),
            )
            for topic in topics
            if topic.enabled
        )

    def _today(self) -> date:
        return self.application_clock.today() if self.application_clock else date.today()

    def _now(self) -> datetime:
        return self.application_clock.now() if self.application_clock else datetime.now(UTC)

    async def _fetch_research_documents(
        self,
        candidate: EventCandidate,
        page: FetchedPage,
        page_text: str,
        page_links: tuple[PageLink, ...],
        *,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[tuple[EvidenceDocument, ...], tuple[str, ...]]:
        primary_text = self._candidate_evidence_text(candidate, page_text)
        documents = [EvidenceDocument(page.url, primary_text)]
        notes: list[str] = []
        visited = {page.url.rstrip("/")}
        queue = [
            (link, 1)
            for link in self._relevant_research_links(candidate.title, page.url, page_links)
        ]
        reserve: tuple[PageLink, int] | None = None

        async def fetch_link(link: PageLink, depth: int) -> tuple[PageLink, ...]:
            normalized_url = link.url.rstrip("/")
            if normalized_url in visited:
                return ()
            visited.add(normalized_url)
            await self._report(progress, f"Проверяю связанную страницу: {link.url}")
            try:
                related = await self.fetcher.fetch(link.url, max_bytes=self.max_page_bytes)
                related_text = extract_page_text(
                    related.html,
                    max_chars=self.max_page_chars,
                )
            except (FetchError, ValueError) as exc:
                code = getattr(exc, "code", type(exc).__name__)
                notes.append(f"Не удалось открыть связанную страницу {link.url}: {code}")
                return ()
            documents.append(EvidenceDocument(related.url, related_text))
            if depth >= 2:
                return ()
            related_links = extract_page_links(related.html, related.url)
            return self._relevant_research_links(
                candidate.title,
                related.url,
                related_links,
            )

        while queue and len(documents) < 7:
            link, depth = queue.pop(0)
            if self._research_link_score(candidate.title, link) <= 0:
                reserve = reserve or (link, depth)
                continue
            children = await fetch_link(link, depth)
            queue.extend((child, depth + 1) for child in children)
        if (
            reserve is not None
            and len(documents) < 7
            and not self._documents_have_temporal_evidence(documents)
        ):
            await fetch_link(*reserve)
        return tuple(documents), tuple(notes)

    @staticmethod
    async def _report(
        progress: Callable[[str], Awaitable[None]] | None,
        text: str,
    ) -> None:
        if progress is not None:
            await progress(text)

    @staticmethod
    def _documents_text(documents: tuple[EvidenceDocument, ...]) -> str:
        return "\n\n".join(
            f"DOCUMENT_URL: {document.url}\nDOCUMENT_TEXT:\n{document.text}\nEND_DOCUMENT"
            for document in documents
        )

    @staticmethod
    def _relevant_research_links(
        expected_name: str,
        page_url: str,
        links: tuple[PageLink, ...],
    ) -> tuple[PageLink, ...]:
        ranked: list[tuple[int, int, PageLink]] = []
        reserve: list[tuple[float, int, PageLink]] = []
        seen_urls: set[str] = set()
        page_host = urlsplit(page_url).netloc.casefold()
        for position, link in enumerate(links):
            if link.url.rstrip("/") == page_url.rstrip("/") or link.url in seen_urls:
                continue
            if urlsplit(link.url).netloc.casefold() != page_host:
                continue
            path = urlsplit(link.url).path.casefold()
            if path.endswith((".pdf", ".jpg", ".jpeg", ".png", ".webp")):
                continue
            seen_urls.add(link.url)
            score = ConferenceResearchService._research_link_score(expected_name, link)
            if score > 0:
                ranked.append((score, -position, link))
            else:
                similarity = SequenceMatcher(
                    None, normalize_name(expected_name), normalize_name(link.text)
                ).ratio()
                reserve.append((similarity, -position, link))
        ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
        reserve.sort(reverse=True, key=lambda item: (item[0], item[1]))
        selected = [item[2] for item in ranked[:3]]
        if reserve:
            selected.append(reserve[0][2])
        return tuple(selected)

    @staticmethod
    def _research_link_score(expected_name: str, link: PageLink) -> int:
        markers = (
            "important date",
            "deadline",
            "registration",
            "abstract",
            "call for paper",
            "call for contribution",
            "programme",
            "program",
            "schedule",
            "venue",
            "location",
            "conference",
            "meeting",
            "важн дат",
            "срок",
            "регистрац",
            "тезис",
            "программ",
            "место",
            "конференц",
        )
        label = normalize_name(link.text)
        url_text = normalize_name(link.url)
        score = sum(3 for marker in markers if marker in label)
        score += sum(1 for marker in markers if marker in url_text)
        if SequenceMatcher(None, normalize_name(expected_name), label).ratio() >= 0.55:
            score += 4
        return score

    @staticmethod
    def _documents_have_temporal_evidence(documents: list[EvidenceDocument]) -> bool:
        text = " ".join(document.text.casefold() for document in documents)
        has_year = re.search(r"\b20\d{2}\b", text) is not None
        has_marker = any(
            marker in text
            for marker in (
                "deadline",
                "important date",
                "registration",
                "abstract",
                "дедлайн",
                "срок",
                "регистрац",
                "тезис",
            )
        )
        return has_year or has_marker

    @staticmethod
    def _merge_research(
        catalog: ConferenceResearch,
        detailed: ConferenceResearch,
    ) -> ConferenceResearch:
        facts = list(catalog.facts)
        fact_keys = {(item.kind, item.value, item.excerpt) for item in facts}
        for item in detailed.facts:
            key = (item.kind, item.value, item.excerpt)
            if key not in fact_keys:
                fact_keys.add(key)
                facts.append(item)
        deadlines = list(catalog.deadlines)
        deadline_keys = {
            (item.deadline_type, item.exact_date, item.approximate_year, item.approximate_month)
            for item in deadlines
        }
        for item in detailed.deadlines:
            key = (
                item.deadline_type,
                item.exact_date,
                item.approximate_year,
                item.approximate_month,
            )
            if key not in deadline_keys:
                deadline_keys.add(key)
                deadlines.append(item)
        return detailed.model_copy(
            update={
                "aliases": tuple(dict.fromkeys((*catalog.aliases, *detailed.aliases))),
                "facts": tuple(facts),
                "deadlines": tuple(deadlines),
            }
        )

    @staticmethod
    def _attribute_claim_sources(
        research: VerifiedConferenceResearch,
        documents: tuple[EvidenceDocument, ...],
    ) -> VerifiedConferenceResearch:
        def source_for(claim) -> str:
            excerpt = " ".join(claim.excerpt.casefold().split())
            matches = [
                document.url
                for document in documents
                if excerpt and excerpt in " ".join(document.text.casefold().split())
            ]
            return matches[-1] if matches else str(claim.source_url)

        facts = tuple(
            FactClaim.model_validate(
                {**claim.model_dump(mode="python"), "source_url": source_for(claim)}
            )
            for claim in research.facts
        )
        deadlines = tuple(
            type(claim).model_validate(
                {**claim.model_dump(mode="python"), "source_url": source_for(claim)}
            )
            for claim in research.deadlines
        )
        return research.model_copy(update={"facts": facts, "deadlines": deadlines})

    _MONTHS: ClassVar[dict[str, int]] = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    _MONTH_DATE_RANGE: ClassVar[re.Pattern[str]] = re.compile(
        r"\b(?P<month>January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+"
        r"(?P<start>\d{1,2})\s*[\-–—]\s*(?P<end>\d{1,2})\s*,?\s*"
        r"(?P<year>20\d{2})\b",
        re.IGNORECASE,
    )

    @classmethod
    def _recover_visible_event_dates(
        cls,
        research: VerifiedConferenceResearch,
        evidence_text: str,
        expected_name: str,
        source_url: str,
    ) -> VerifiedConferenceResearch:
        if any(
            fact.kind.casefold() in {"start_date", "end_date", "event_dates", "event_date"}
            for fact in research.facts
        ):
            return research

        page_text = evidence_text.rsplit("FETCHED_PAGE_TEXT:\n", maxsplit=1)[-1]
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        if not lines:
            return research

        expected = normalize_name(expected_name)
        anchor_indexes = [
            index
            for index, line in enumerate(lines)
            if SequenceMatcher(None, expected, normalize_name(line)).ratio() >= 0.55
        ]
        if not anchor_indexes:
            return research

        selected: tuple[int, re.Match[str]] | None = None
        for anchor in anchor_indexes:
            for index in range(max(0, anchor - 2), min(len(lines), anchor + 5)):
                match = cls._MONTH_DATE_RANGE.search(lines[index])
                if match is None:
                    continue
                distance = abs(index - anchor)
                if selected is None or distance < selected[0]:
                    selected = distance, match
        if selected is None:
            return research

        match = selected[1]
        month = cls._MONTHS[match.group("month").casefold()]
        year = int(match.group("year"))
        try:
            start = date(year, month, int(match.group("start")))
            end = date(year, month, int(match.group("end")))
        except ValueError:
            return research
        if end < start:
            return research

        source_class = research.source_class
        verification_status = {
            EvidenceSourceClass.OFFICIAL_CONFERENCE: VerificationStatus.OFFICIAL_CONFIRMED,
            EvidenceSourceClass.OFFICIAL_ORGANIZER: VerificationStatus.ORGANIZER_CONFIRMED,
            EvidenceSourceClass.SECONDARY: VerificationStatus.SECONDARY_UNVERIFIED,
        }[source_class]
        excerpt = match.string.strip()
        recovered = (
            FactClaim(
                kind="start_date",
                value=start.isoformat(),
                source_url=source_url,
                source_class=source_class,
                excerpt=excerpt,
                verification_status=verification_status,
            ),
            FactClaim(
                kind="end_date",
                value=end.isoformat(),
                source_url=source_url,
                source_class=source_class,
                excerpt=excerpt,
                verification_status=verification_status,
            ),
        )
        return research.model_copy(update={"facts": (*research.facts, *recovered)})

    async def _fetch_candidate_page(
        self, candidate: EventCandidate
    ) -> tuple[FetchedPage, str, tuple[PageLink, ...]]:
        page = await self.fetcher.fetch(candidate.source_url, max_bytes=self.max_page_bytes)
        text = extract_page_text(page.html, max_chars=self.max_page_chars)
        links = extract_page_links(page.html, page.url)
        return page, text, links

    @staticmethod
    def _candidate_evidence_text(candidate: EventCandidate, page_text: str) -> str:
        summary = candidate.summary or ""
        return (
            "SEARCH_RESULT_CONTEXT (secondary, unverified):\n"
            f"Title: {candidate.title}\n"
            f"Snippet: {summary}\n"
            "END_SEARCH_RESULT_CONTEXT\n\n"
            "FETCHED_PAGE_TEXT:\n"
            f"{page_text}"
        )

    @classmethod
    def _downgrade_catalog_snippet_evidence(
        cls,
        catalog: ConferenceCatalogResearch,
        summary: str | None,
    ) -> ConferenceCatalogResearch:
        if not summary:
            return catalog
        return catalog.model_copy(
            update={
                "events": tuple(
                    cls._downgrade_snippet_evidence(event, summary) for event in catalog.events
                )
            }
        )

    @staticmethod
    def _downgrade_snippet_evidence(research, summary: str | None):
        if not summary:
            return research
        normalized_summary = " ".join(summary.casefold().split())

        def from_snippet(claim) -> bool:
            excerpt = " ".join(claim.excerpt.casefold().split())
            return bool(excerpt) and excerpt in normalized_summary

        facts = tuple(
            claim.model_copy(
                update={
                    "source_class": EvidenceSourceClass.SECONDARY,
                    "verification_status": VerificationStatus.SECONDARY_UNVERIFIED,
                }
            )
            if from_snippet(claim)
            else claim
            for claim in research.facts
        )
        deadlines = tuple(
            claim.model_copy(
                update={
                    "source_class": EvidenceSourceClass.SECONDARY,
                    "verification_status": (
                        VerificationStatus.TENTATIVE
                        if claim.tentative
                        else VerificationStatus.SECONDARY_UNVERIFIED
                    ),
                }
            )
            if from_snippet(claim)
            else claim
            for claim in research.deadlines
        )
        return research.model_copy(update={"facts": facts, "deadlines": deadlines})

    async def _extract_catalog(
        self, source_url: str, text: str
    ) -> ConferenceCatalogResearch | None:
        extractor = getattr(self.ai, "extract_conference_catalog", None)
        if extractor is None:
            return None
        return await extractor(source_url, text)

    async def _expand_catalog(
        self,
        parent: EventCandidate,
        catalog: ConferenceCatalogResearch,
        page_links: tuple[PageLink, ...],
    ) -> CatalogExpansion:
        usable_events = [event for event in catalog.events if self._is_usable_event(event)]
        if not usable_events:
            raise CandidateUnusableError("catalog contains no concrete events with dates")
        drafts = [
            CandidateDraft(
                title=event.name,
                source_url=(
                    matched.url
                    if (matched := self._best_event_link(event.name, page_links)) is not None
                    else parent.source_url
                ),
                query=parent.query,
                relevance_score=parent.relevance_score,
                verification_status=self._candidate_verification_status(event),
                summary=self._event_summary(event),
            )
            for event in usable_events
        ]
        children = await self.repository.create_candidates(
            parent.user_id,
            drafts,
            expires_at=parent.expires_at,
        )
        events_by_name = {normalize_name(event.name): event for event in usable_events}
        for child in children:
            event = events_by_name.get(child.normalized_title)
            if event is not None:
                await self.repository.save_candidate_preview(parent.user_id, child.id, event)
        child_ids = tuple(item.id for item in children if item.id != parent.id)
        if not child_ids:
            raise CandidateUnusableError("catalog contains no new concrete events")
        await self.repository.transition_candidate(
            parent.user_id,
            parent.id,
            CandidateStatus.SKIPPED,
            CandidateDecisionReason.CATALOG_EXPANDED,
            CandidateDecisionSource.SYSTEM,
            self._now(),
        )
        await self.session.commit()
        return CatalogExpansion(parent.id, child_ids)

    @staticmethod
    def _best_event_link(
        event_name: str,
        links: tuple[PageLink, ...],
    ) -> PageLink | None:
        expected = normalize_name(event_name)
        if not expected:
            return None

        def score(link: PageLink) -> float:
            label = normalize_name(link.text)
            if label == expected:
                return 1.0
            if expected in label or label in expected:
                return 0.9
            return SequenceMatcher(None, expected, label).ratio()

        candidates = [link for link in links if normalize_name(link.text)]
        if not candidates:
            return None
        selected = max(candidates, key=score)
        return selected if score(selected) >= 0.55 else None

    @classmethod
    def _should_expand_catalog(
        cls,
        candidate: EventCandidate,
        catalog: ConferenceCatalogResearch,
    ) -> bool:
        if cls._is_generic_title(candidate.title):
            return True
        if len(catalog.events) <= 1:
            return False
        _event, score = cls._best_catalog_event(candidate.title, catalog.events)
        return score < 0.55

    @staticmethod
    def _best_catalog_event(
        candidate_title: str,
        events: tuple[ConferenceResearch, ...],
    ) -> tuple[ConferenceResearch, float]:
        expected = normalize_name(candidate_title)

        def score(event: ConferenceResearch) -> float:
            return SequenceMatcher(None, expected, normalize_name(event.name)).ratio()

        selected = max(events, key=score)
        return selected, score(selected)

    @staticmethod
    def _is_generic_title(title: str) -> bool:
        stripped = title.strip().casefold()
        if re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", stripped):
            return True
        normalized = normalize_name(title)
        markers = (
            "объявления о конференц",
            "список конференц",
            "conference announcements",
            "meeting announcements",
            "conference calendar",
            "events calendar",
            "future meetings",
            "upcoming meetings",
        )
        return any(marker in normalized for marker in markers)

    @classmethod
    def _is_usable_event(cls, research: ConferenceResearch) -> bool:
        if cls._is_generic_title(research.name):
            return False
        has_event_date = any(
            fact.kind.casefold() in {"start_date", "end_date", "event_dates", "event_date"}
            for fact in research.facts
        )
        return has_event_date or bool(research.deadlines)

    @staticmethod
    def _event_summary(research: ConferenceResearch) -> str | None:
        claims = (
            *(item for item in research.facts if item.kind in {"start_date", "end_date"}),
            *research.deadlines,
        )
        excerpts = tuple(
            dict.fromkeys(item.excerpt.strip() for item in claims if item.excerpt.strip())
        )
        return " | ".join(excerpts[:3])[:4_000] or None

    @staticmethod
    def _candidate_identity_exists(
        draft: CandidateDraft,
        identities: list[tuple[str, str]],
    ) -> bool:
        source_identity, normalized_title = ConferenceResearchService._candidate_identity(draft)
        return any(
            (canonical_url(source_url) or source_url) == source_identity
            and (
                stored_title == normalized_title
                or stored_title in normalized_title
                or normalized_title in stored_title
            )
            for source_url, stored_title in identities
        )

    @staticmethod
    def _candidate_identity(draft: CandidateDraft) -> tuple[str, str]:
        return (
            canonical_url(draft.source_url) or draft.source_url,
            normalize_name(draft.title),
        )

    @staticmethod
    def _stored_candidate_identity(candidate: EventCandidate) -> tuple[str, str]:
        return (
            canonical_url(candidate.source_url) or candidate.source_url,
            candidate.normalized_title,
        )

    @staticmethod
    def _research_signature(
        research: VerifiedConferenceResearch | None,
    ) -> tuple[tuple[str, str], ...]:
        if research is None:
            return ()
        values: list[tuple[str, str]] = [
            (item.kind, item.value)
            for item in research.facts
            if item.kind
            in {
                "start_date",
                "end_date",
                "program",
            }
        ]
        values.extend(
            (
                f"deadline:{item.deadline_type.value}",
                item.exact_date.isoformat()
                if item.exact_date is not None
                else f"{item.approximate_year}-{item.approximate_month or 0:02d}",
            )
            for item in research.deadlines
        )
        return tuple(sorted(values))

    @staticmethod
    def _candidate_verification_status(
        research: ConferenceResearch,
    ) -> VerificationStatus:
        statuses = [item.verification_status for item in (*research.facts, *research.deadlines)]
        for status in (
            VerificationStatus.OFFICIAL_CONFIRMED,
            VerificationStatus.ORGANIZER_CONFIRMED,
            VerificationStatus.TENTATIVE,
            VerificationStatus.SECONDARY_UNVERIFIED,
        ):
            if status in statuses:
                return status
        return VerificationStatus.UNVERIFIABLE
