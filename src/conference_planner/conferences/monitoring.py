import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from typing import ClassVar, Protocol
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.ai.gateway import ModelGatewayError
from conference_planner.ai.service import AIResponseError
from conference_planner.conferences.deduplication import normalize_name
from conference_planner.conferences.evidence import (
    EvidenceDocument,
    EvidenceValidator,
    verification_required,
)
from conference_planner.conferences.persistence import (
    ConferencePersistenceService,
    PersistenceReceipt,
)
from conference_planner.conferences.temporal import (
    enrich_catalog_events,
    extract_catalog_link_events,
    extract_temporal_research,
    merge_temporal_research,
)
from conference_planner.db.base import utc_now
from conference_planner.db.models import (
    Conference,
    ConferenceChange,
    ConferenceEvidence,
    Deadline,
    MonitoringAttempt,
    Subscription,
    TrustedSource,
)
from conference_planner.db.repositories.conference_research import (
    CandidateDraft,
    ConferenceResearchRepository,
)
from conference_planner.db.repositories.parser_recipes import ParserRecipeRepository
from conference_planner.db.repositories.web_cache import WebDocumentCacheRepository
from conference_planner.schemas.common import (
    CandidateStatus,
    ConferenceStatus,
    DeadlineType,
    EvidenceSourceClass,
    MonitoringAttemptStatus,
    SourceType,
    VerificationStatus,
)
from conference_planner.schemas.integrations import SearchResult
from conference_planner.schemas.parser_recipes import PageKind
from conference_planner.schemas.research import (
    ConferenceCatalogResearch,
    ConferenceResearch,
    DeadlineClaim,
    FactClaim,
    VerifiedConferenceResearch,
)
from conference_planner.search.provider import SearchProvider
from conference_planner.web.fetcher import FetchedPage, FetchError, normalized_content_hash
from conference_planner.web.access import should_try_reader
from conference_planner.web.parser import PageLink, extract_page_links, extract_page_text
from conference_planner.web.recipes import RecipeValidationError, apply_recipe

logger = logging.getLogger(__name__)


class MonitoringFetcherProtocol(Protocol):
    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchedPage: ...


class MonitoringAnalyzer(Protocol):
    async def extract_conference_research(
        self, source_url: str, text: str, expected_name: str
    ) -> ConferenceResearch: ...

    async def verify_conference_research(
        self, research: ConferenceResearch
    ) -> VerifiedConferenceResearch: ...


class MonitoringReaderProtocol(Protocol):
    async def read(self, url: str, *, max_bytes: int) -> str: ...


class MonitoringCheckError(RuntimeError):
    pass


class MonitoringNotDue(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogConversion:
    conference_id: int
    conference_name: str
    source_url: str
    candidate_ids: tuple[int, ...]
    committed: bool = True

    def telegram_summary(self) -> str:
        return (
            "Страница распознана как каталог мероприятий. "
            "Она перенесена в источники, исходная запись убрана из отслеживаемых. "
            f"Кандидатов добавлено: {len(self.candidate_ids)}."
        )


@dataclass(frozen=True, slots=True)
class _CatalogPage:
    source_url: str
    catalog: ConferenceCatalogResearch
    links: tuple[PageLink, ...]


@dataclass(frozen=True, slots=True)
class DeadlineChange:
    conference_id: int
    conference_name: str
    deadline_type: DeadlineType
    old_date: date
    new_date: date
    source_url: str


@dataclass(frozen=True, slots=True)
class MonitoringFailure:
    conference_id: int
    error_code: str
    consecutive_failures: int


@dataclass(slots=True)
class MonitoringReport:
    created: list[PersistenceReceipt] = field(default_factory=list)
    changed: list[PersistenceReceipt] = field(default_factory=list)
    conflicts: list[PersistenceReceipt] = field(default_factory=list)
    unchanged: list[PersistenceReceipt] = field(default_factory=list)
    changes: list[DeadlineChange] = field(default_factory=list)
    errors: list[MonitoringFailure] = field(default_factory=list)
    catalogs: list[CatalogConversion] = field(default_factory=list)


class MonitoringService:
    _locks: ClassVar[dict[tuple[str, int], asyncio.Lock]] = {}

    def __init__(
        self,
        session: AsyncSession,
        fetcher: MonitoringFetcherProtocol,
        ai: MonitoringAnalyzer,
        max_page_bytes: int,
        max_page_chars: int,
        search: SearchProvider | None = None,
        *,
        cache: WebDocumentCacheRepository | None = None,
        recipes: ParserRecipeRepository | None = None,
        reader: MonitoringReaderProtocol | None = None,
        monitoring_interval_hours: int = 72,
        now_factory: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session = session
        self.fetcher = fetcher
        self.ai = ai
        self.max_page_bytes = max_page_bytes
        self.max_page_chars = max_page_chars
        self.search = search
        self.cache = cache
        self.recipes = recipes
        self.reader = reader
        self.monitoring_interval = timedelta(hours=monitoring_interval_hours)
        self.now_factory = now_factory
        self.attempts = ConferenceResearchRepository(session)

    async def check_conference(
        self,
        user_id: int,
        conference_id: int,
        *,
        force: bool = True,
    ) -> PersistenceReceipt | CatalogConversion:
        key = (str(self.session.get_bind().url), conference_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            conference = await self.session.scalar(
                select(Conference)
                .join(Subscription, Subscription.conference_id == Conference.id)
                .where(
                    Conference.id == conference_id,
                    Conference.is_test.is_(False),
                    Conference.is_catalog_source.is_(False),
                    Subscription.user_id == user_id,
                    Subscription.status == ConferenceStatus.TRACKING,
                )
            )
            if conference is None:
                raise ValueError("tracked conference not found")
            if not force and not self._is_due(conference.last_checked_at):
                raise MonitoringNotDue

            has_stored_deadline = await self.session.scalar(
                select(Deadline.id).where(Deadline.conference_id == conference.id).limit(1)
            )
            recover_missing_temporal = not (
                conference.start_date or conference.end_date or has_stored_deadline
            )
            reanalyze_unchanged = recover_missing_temporal or self._looks_like_catalog_name(
                conference.name
            )

            source_urls = await self._source_urls(conference)
            urls = self._monitoring_urls(source_urls)
            errors: list[str] = []
            event_date_research: VerifiedConferenceResearch | None = None
            fallback_research: VerifiedConferenceResearch | None = None
            empty_research: VerifiedConferenceResearch | None = None
            unchanged_url: str | None = None
            for url in urls:
                try:
                    verified = await self._research_url(
                        url,
                        conference.name,
                        reanalyze_unchanged=reanalyze_unchanged,
                    )
                    if isinstance(verified, _CatalogPage):
                        return await self._convert_catalog(user_id, conference, verified)
                    if verified is None:
                        unchanged_url = unchanged_url or url
                        continue
                    if self._has_deadlines(verified):
                        verified = self._merge_research(event_date_research, verified)
                        return await self._persist_attempt(user_id, conference, verified)
                    if self._has_event_dates(verified):
                        event_date_research = self._merge_research(event_date_research, verified)
                        continue
                    if self._has_claims(verified):
                        fallback_research = fallback_research or verified
                    else:
                        empty_research = empty_research or verified
                except Exception as exc:
                    errors.append(self._error_code(exc))

            if self.search is not None:
                results: list[SearchResult] = []
                seen_result_urls: set[str] = set()
                for query in self._search_queries(conference.name, source_urls):
                    try:
                        query_results = await self.search.search(query)
                    except Exception as exc:
                        errors.append(self._error_code(exc))
                        continue
                    for result in query_results[:5]:
                        result_url = str(result.url)
                        if result_url in seen_result_urls:
                            continue
                        seen_result_urls.add(result_url)
                        results.append(result)
                        if len(results) >= 10:
                            break
                    if len(results) >= 10:
                        break
                for result in results:
                    url = str(result.url)
                    verified: VerifiedConferenceResearch | None = None
                    if url not in urls:
                        try:
                            verified = await self._research_url(
                                url,
                                conference.name,
                                reanalyze_unchanged=reanalyze_unchanged,
                            )
                        except Exception as exc:
                            errors.append(self._error_code(exc))
                    if isinstance(verified, _CatalogPage):
                        return await self._convert_catalog(user_id, conference, verified)
                    if verified is not None and self._has_deadlines(verified):
                        verified = self._merge_research(event_date_research, verified)
                        return await self._persist_attempt(user_id, conference, verified)
                    if verified is not None and self._has_event_dates(verified):
                        event_date_research = self._merge_research(event_date_research, verified)
                    if result.snippet:
                        try:
                            snippet_research = await self._research_search_result(
                                result, conference.name
                            )
                        except Exception as exc:
                            errors.append(self._error_code(exc))
                        else:
                            if self._has_deadlines(snippet_research):
                                snippet_research = self._merge_research(
                                    event_date_research, snippet_research
                                )
                                return await self._persist_attempt(
                                    user_id, conference, snippet_research
                                )
                            if self._has_event_dates(snippet_research):
                                event_date_research = self._merge_research(
                                    event_date_research, snippet_research
                                )
                            elif self._has_claims(snippet_research):
                                fallback_research = fallback_research or snippet_research
                            else:
                                empty_research = empty_research or snippet_research
                    elif verified is not None and not self._has_event_dates(verified):
                        if self._has_claims(verified):
                            fallback_research = fallback_research or verified
                        else:
                            empty_research = empty_research or verified

            if event_date_research is not None:
                return await self._persist_attempt(user_id, conference, event_date_research)
            if unchanged_url is not None:
                return await self._record_unchanged(user_id, conference, unchanged_url)

            error_code = (
                "NO_TEMPORAL_DATA"
                if fallback_research is not None or empty_research is not None
                else errors[-1]
                if errors
                else "NO_TEMPORAL_DATA"
            )
            await self.attempts.record_attempt(
                user_id,
                conference.id,
                MonitoringAttemptStatus.FAILED,
                error_code=error_code,
            )
            await self.session.commit()
            raise MonitoringCheckError(error_code)

    async def run(self, user_id: int, *, force: bool = False) -> MonitoringReport:
        report = MonitoringReport()
        cutoff = self.now_factory() - self.monitoring_interval
        conference_ids = list(
            await self.session.scalars(
                select(Conference.id)
                .join(Subscription, Subscription.conference_id == Conference.id)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.status == ConferenceStatus.TRACKING,
                    Conference.is_test.is_(False),
                    Conference.is_catalog_source.is_(False),
                    *(
                        ()
                        if force
                        else (
                            (Conference.last_checked_at.is_(None))
                            | (Conference.last_checked_at <= cutoff),
                        )
                    ),
                )
                .order_by(Conference.id)
            )
        )
        for conference_id in conference_ids:
            try:
                receipt = await self.check_conference(user_id, conference_id, force=force)
            except MonitoringNotDue:
                continue
            except MonitoringCheckError as exc:
                report.errors.append(
                    MonitoringFailure(
                        conference_id,
                        str(exc),
                        await self._consecutive_failures(user_id, conference_id),
                    )
                )
                continue
            if isinstance(receipt, CatalogConversion):
                report.catalogs.append(receipt)
                continue
            attempt_status = await self._latest_attempt_status(user_id, conference_id)
            if attempt_status == MonitoringAttemptStatus.CONFLICT:
                report.conflicts.append(receipt)
            elif receipt.updated_fields:
                report.changed.append(receipt)
            elif receipt.created_fields:
                report.created.append(receipt)
            else:
                report.unchanged.append(receipt)
            report.changes.extend(await self._deadline_changes(receipt))
        return report

    async def _persist_attempt(
        self,
        user_id: int,
        conference: Conference,
        verified: VerifiedConferenceResearch,
    ) -> PersistenceReceipt:
        receipt = await ConferencePersistenceService(self.session).persist_for_conference(
            user_id, conference.id, verified
        )
        stored_statuses = set(
            await self.session.scalars(
                select(ConferenceEvidence.verification_status).where(
                    ConferenceEvidence.id.in_(receipt.evidence_ids)
                )
            )
        )
        has_conflict = VerificationStatus.CONFLICT in stored_statuses or any(
            item.verification_status == VerificationStatus.CONFLICT
            for item in (*verified.facts, *verified.deadlines)
        )
        if has_conflict:
            status = MonitoringAttemptStatus.CONFLICT
        elif receipt.created_fields or receipt.updated_fields:
            status = MonitoringAttemptStatus.SUCCEEDED
        else:
            status = MonitoringAttemptStatus.UNCHANGED
        await self.attempts.record_attempt(
            user_id,
            conference.id,
            status,
            source_url=str(verified.source_url),
        )
        await self.session.commit()
        return receipt

    async def _source_urls(self, conference: Conference) -> list[str]:
        evidence_urls = list(
            await self.session.scalars(
                select(ConferenceEvidence.source_url)
                .where(
                    ConferenceEvidence.conference_id == conference.id,
                    ConferenceEvidence.active.is_(True),
                )
                .order_by(ConferenceEvidence.observed_at.desc(), ConferenceEvidence.id.desc())
            )
        )
        deadline_urls = list(
            await self.session.scalars(
                select(Deadline.source_url)
                .where(Deadline.conference_id == conference.id)
                .order_by(Deadline.id.desc())
            )
        )
        return list(
            dict.fromkeys(
                filter(
                    None,
                    (
                        conference.website,
                        conference.source_url,
                        *evidence_urls,
                        *deadline_urls,
                    ),
                )
            )
        )

    async def _latest_attempt_status(
        self, user_id: int, conference_id: int
    ) -> MonitoringAttemptStatus | None:
        return await self.session.scalar(
            select(MonitoringAttempt.status)
            .where(
                MonitoringAttempt.user_id == user_id,
                MonitoringAttempt.conference_id == conference_id,
            )
            .order_by(MonitoringAttempt.observed_at.desc(), MonitoringAttempt.id.desc())
            .limit(1)
        )

    async def _research_url(
        self,
        url: str,
        expected_name: str,
        *,
        reanalyze_unchanged: bool = False,
    ) -> VerifiedConferenceResearch | _CatalogPage | None:
        cached = await self.cache.get(url) if self.cache is not None else None
        fetch_kwargs: dict[str, object] = {"max_bytes": self.max_page_bytes}
        if cached is not None:
            fetch_kwargs.update(etag=cached.etag, last_modified=cached.last_modified)
        try:
            page = await self.fetcher.fetch(url, **fetch_kwargs)
        except FetchError as exc:
            reader_text: str | None = None
            if self.reader is not None and should_try_reader(exc.code):
                try:
                    reader_text = await self.reader.read(url, max_bytes=self.max_page_bytes)
                except Exception:
                    reader_text = None
            if reader_text:
                result = await self._analyze_page_text(
                    url, reader_text[: self.max_page_chars], expected_name
                )
                if self.cache is not None:
                    await self.cache.store(
                        FetchedPage(
                            url,
                            reader_text,
                            content_hash=normalized_content_hash(reader_text),
                        ),
                        reader_text[: self.max_page_chars],
                        observed_at=self.now_factory(),
                    )
                return result
            if exc.code in {"CAPTCHA_REQUIRED", "ACCESS_DENIED", "RATE_LIMITED"}:
                raise
            if cached is None or not cached.extracted_text.strip():
                raise
            logger.warning("using stale cache for inaccessible source %s (reason: %s); results are not freshly verified", url, exc.code)
            return await self._analyze_page_text(url, cached.extracted_text, expected_name)
        if cached is not None and page.not_modified:
            await self.cache.touch(
                cached,
                etag=page.etag,
                last_modified=page.last_modified,
                status_code=page.status_code,
                observed_at=self.now_factory(),
            )
            if reanalyze_unchanged and cached.extracted_text.strip():
                if self._looks_like_catalog_name(expected_name):
                    return await self._refetch_cached_catalog(
                        url, cached.extracted_text, expected_name
                    )
                return await self._analyze_page_text(url, cached.extracted_text, expected_name)
            return None
        if cached is not None and page.content_hash == cached.content_hash:
            await self.cache.touch(
                cached,
                etag=page.etag,
                last_modified=page.last_modified,
                status_code=page.status_code,
                observed_at=self.now_factory(),
            )
            if reanalyze_unchanged and cached.extracted_text.strip():
                return await self._analyze_page_text(
                    page.url,
                    cached.extracted_text,
                    expected_name,
                    links=extract_page_links(page.html, page.url),
                )
            return None
        text = extract_page_text(page.html, max_chars=self.max_page_chars)
        text = await self._apply_active_recipe(page, text)
        verified = await self._analyze_page_text(
            page.url,
            text,
            expected_name,
            links=extract_page_links(page.html, page.url),
        )
        if self.cache is not None:
            await self.cache.store(page, text, observed_at=self.now_factory())
        return verified

    async def _analyze_page_text(
        self,
        source_url: str,
        text: str,
        expected_name: str,
        *,
        links: tuple[PageLink, ...] = (),
    ) -> VerifiedConferenceResearch | _CatalogPage:
        catalog = await self._extract_catalog(source_url, text, expected_name, links)
        if catalog is not None:
            return _CatalogPage(source_url, catalog, links)
        if self._looks_like_catalog_name(expected_name):
            return VerifiedConferenceResearch(
                name=expected_name,
                source_url=source_url,
                source_class=EvidenceSourceClass.OFFICIAL_ORGANIZER,
            )
        return await self._analyze_text(source_url, text, expected_name)

    async def _refetch_cached_catalog(
        self,
        url: str,
        cached_text: str,
        expected_name: str,
    ) -> VerifiedConferenceResearch | _CatalogPage:
        try:
            page = await self.fetcher.fetch(url, max_bytes=self.max_page_bytes)
        except FetchError:
            return await self._analyze_page_text(url, cached_text, expected_name)
        if page.not_modified or not page.html.strip():
            return await self._analyze_page_text(url, cached_text, expected_name)
        text = extract_page_text(page.html, max_chars=self.max_page_chars)
        text = await self._apply_active_recipe(page, text)
        result = await self._analyze_page_text(
            page.url,
            text,
            expected_name,
            links=extract_page_links(page.html, page.url),
        )
        if self.cache is not None:
            await self.cache.store(page, text, observed_at=self.now_factory())
        return result

    async def _extract_catalog(
        self,
        source_url: str,
        text: str,
        expected_name: str,
        links: tuple[PageLink, ...],
    ) -> ConferenceCatalogResearch | None:
        if not self._looks_like_catalog_name(expected_name):
            return None
        link_catalog = extract_catalog_link_events(
            source_url,
            expected_name,
            links,
            min_year=self.now_factory().year,
        )
        extractor = getattr(self.ai, "extract_conference_catalog", None)
        catalog = ConferenceCatalogResearch(events=())
        if extractor is not None:
            try:
                catalog = await extractor(source_url, text)
            except (AIResponseError, ModelGatewayError):
                logger.warning(
                    "catalog model extraction failed; using deterministic links",
                    extra={"link_events": len(link_catalog.events)},
                )
        model_events = [
            event
            for event in catalog.events
            if event.deadlines
            or any(claim.kind in {"start_date", "end_date"} for claim in event.facts)
        ]
        by_name = {normalize_name(event.name): event for event in model_events}
        for event in link_catalog.events:
            by_name.setdefault(normalize_name(event.name), event)
        usable = enrich_catalog_events(
            ConferenceCatalogResearch(events=tuple(by_name.values())),
            source_url,
            text,
            links,
        ).events
        if len(usable) < 2:
            return None
        logger.info(
            "catalog events extracted",
            extra={
                "model_events": len(model_events),
                "link_events": len(link_catalog.events),
                "usable_events": len(usable),
            },
        )
        return ConferenceCatalogResearch(events=usable)

    async def _convert_catalog(
        self,
        user_id: int,
        conference: Conference,
        page: _CatalogPage,
    ) -> CatalogConversion:
        source = await self.session.scalar(
            select(TrustedSource).where(TrustedSource.url == page.source_url)
        )
        if source is None:
            source = TrustedSource(
                url=page.source_url,
                name=conference.name[:300],
                source_type=SourceType.PAGE,
                enabled=True,
            )
            self.session.add(source)
        source.last_checked_at = self.now_factory()
        conference.is_catalog_source = True
        drafts = [
            CandidateDraft(
                title=event.name,
                source_url=(
                    str(event.source_url)
                    if str(event.source_url).rstrip("/") != page.source_url.rstrip("/")
                    else self._event_link(event.name, page.links) or page.source_url
                ),
                query=f"Каталог: {conference.name}",
                relevance_score=1.0,
                verification_status=self._event_verification_status(event),
                summary=self._event_summary(event),
            )
            for event in page.catalog.events
        ]
        candidates = await self.attempts.create_candidates(
            user_id,
            drafts,
            expires_at=self.now_factory() + timedelta(days=7),
        )
        events_by_name = {
            normalize_name(event.name): event for event in page.catalog.events
        }
        for candidate in candidates:
            event = events_by_name.get(candidate.normalized_title)
            if event is not None:
                await self.attempts.save_candidate_preview(user_id, candidate.id, event)
        subscription = await self.session.scalar(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.conference_id == conference.id,
            )
        )
        if subscription is not None:
            await self.session.delete(subscription)
        conference.start_date = None
        conference.end_date = None
        conference.location = None
        conference.status = ConferenceStatus.ARCHIVED
        conference.last_checked_at = self.now_factory()
        await self.attempts.record_attempt(
            user_id,
            conference.id,
            MonitoringAttemptStatus.SUCCEEDED,
            source_url=page.source_url,
        )
        await self.session.commit()
        return CatalogConversion(
            conference.id,
            conference.name,
            page.source_url,
            tuple(
                candidate.id
                for candidate in candidates
                if candidate.status == CandidateStatus.ACTIVE
            ),
        )

    async def _research_search_result(
        self, result: SearchResult, expected_name: str
    ) -> VerifiedConferenceResearch:
        text = f"{result.title}\n{result.snippet or ''}".strip()
        return await self._analyze_text(str(result.url), text, expected_name, snippet_only=True)

    async def _analyze_text(
        self,
        source_url: str,
        text: str,
        expected_name: str,
        *,
        snippet_only: bool = False,
    ) -> VerifiedConferenceResearch:
        deterministic = extract_temporal_research(source_url, text, expected_name)
        try:
            model_research = await self.ai.extract_conference_research(
                source_url, text, expected_name
            )
        except (AIResponseError, ModelGatewayError):
            if not (*deterministic.facts, *deterministic.deadlines):
                raise
            logger.warning(
                "model temporal extraction failed; using deterministic evidence",
                extra={
                    "deterministic_facts": len(deterministic.facts),
                    "deterministic_deadlines": len(deterministic.deadlines),
                },
            )
            extracted = deterministic
        else:
            extracted = merge_temporal_research(model_research, deterministic)
        if snippet_only:
            extracted = self._as_secondary(extracted)
        documents = (EvidenceDocument(source_url, text),)
        validator = EvidenceValidator()
        deterministic_identities = {
            self._claim_identity(claim)
            for claim in (*deterministic.facts, *deterministic.deadlines)
        }
        needs_model_verification = any(
            verification_required(claim, validator.context_for(claim, documents))
            for claim in (*extracted.facts, *extracted.deadlines)
            if snippet_only or self._claim_identity(claim) not in deterministic_identities
        )
        if needs_model_verification:
            try:
                verified = await self.ai.verify_conference_research(extracted)
            except (AIResponseError, ModelGatewayError):
                if not deterministic_identities:
                    raise
                logger.warning(
                    "model temporal verification failed; retaining cited deterministic evidence",
                    extra={
                        "deterministic_claims": len(deterministic_identities),
                        "total_claims": len(extracted.facts) + len(extracted.deadlines),
                    },
                )
                verified = VerifiedConferenceResearch.model_validate(
                    extracted.model_dump(mode="python")
                )
        else:
            verified = VerifiedConferenceResearch.model_validate(
                extracted.model_dump(mode="python")
            )
        if snippet_only:
            verified = self._as_secondary(verified)
        verified = validator.validate(verified, documents).validated
        logger.info(
            "temporal evidence validated",
            extra={
                "deterministic_facts": len(deterministic.facts),
                "deterministic_deadlines": len(deterministic.deadlines),
                "accepted_facts": len(verified.facts),
                "accepted_deadlines": len(verified.deadlines),
            },
        )
        return verified

    @staticmethod
    def _claim_identity(claim: FactClaim | DeadlineClaim) -> tuple[object, ...]:
        if isinstance(claim, FactClaim):
            return ("fact", claim.kind, claim.value)
        return (
            "deadline",
            claim.deadline_type,
            claim.exact_date,
            claim.approximate_year,
            claim.approximate_month,
        )

    @staticmethod
    def _as_secondary(
        research: ConferenceResearch | VerifiedConferenceResearch,
    ) -> ConferenceResearch | VerifiedConferenceResearch:
        payload = research.model_dump(mode="python")
        payload["source_class"] = EvidenceSourceClass.SECONDARY
        for claim in (*payload["facts"], *payload["deadlines"]):
            claim["source_class"] = EvidenceSourceClass.SECONDARY
            if claim["verification_status"] not in {
                VerificationStatus.CONFLICT,
                VerificationStatus.TENTATIVE,
            }:
                claim["verification_status"] = VerificationStatus.SECONDARY_UNVERIFIED
        return type(research).model_validate(payload)

    @staticmethod
    def _monitoring_urls(source_urls: list[str]) -> list[str]:
        urls: list[str] = []
        for source_url in source_urls:
            urls.append(source_url)
            parsed = urlsplit(source_url)
            path_parts = [part for part in parsed.path.split("/") if part]
            if not parsed.path.endswith("/") or not path_parts:
                continue
            urls.extend(
                urljoin(source_url, suffix)
                for suffix in (
                    "pages/important-dates.php",
                    "important-dates/",
                    "important-dates",
                    "dates/",
                    "deadlines/",
                )
            )
        return list(dict.fromkeys(urls))

    @staticmethod
    def _looks_like_catalog_name(name: str) -> bool:
        normalized = normalize_name(name)
        markers = (
            "conference announcements",
            "conference calendar",
            "conferences 20",
            "future meetings",
            "meeting announcements",
            "meetings 20",
            "объявления о конференц",
            "список конференц",
        )
        return any(marker in normalized for marker in markers)

    @staticmethod
    def _event_link(name: str, links: tuple[PageLink, ...]) -> str | None:
        expected = normalize_name(name)
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
        return selected.url if score(selected) >= 0.55 else None

    @staticmethod
    def _event_verification_status(event: ConferenceResearch) -> VerificationStatus:
        statuses = {claim.verification_status for claim in (*event.facts, *event.deadlines)}
        for status in (
            VerificationStatus.OFFICIAL_CONFIRMED,
            VerificationStatus.ORGANIZER_CONFIRMED,
            VerificationStatus.TENTATIVE,
            VerificationStatus.SECONDARY_UNVERIFIED,
        ):
            if status in statuses:
                return status
        return VerificationStatus.UNVERIFIABLE

    @staticmethod
    def _event_summary(event: ConferenceResearch) -> str | None:
        claims = (
            *(claim for claim in event.facts if claim.kind in {"start_date", "end_date"}),
            *event.deadlines,
        )
        excerpts = tuple(
            dict.fromkeys(
                claim.excerpt.strip()
                for claim in claims
                if claim.excerpt.strip()
            )
        )
        return " | ".join(excerpts[:3])[:4_000] or None

    @staticmethod
    def _search_queries(name: str, source_urls: list[str]) -> tuple[str, ...]:
        queries = [f'"{name}" conference dates deadlines']
        year = next(iter(re.findall(r"\b20\d{2}\b", name)), "")
        first_token = next(iter(re.findall(r"[A-Za-zА-Яа-яЁё][\w-]*", name)), "")
        short_name = " ".join(part for part in (first_token, year) if part)
        if short_name and short_name.casefold() != name.casefold():
            queries.append(f'"{short_name}" conference important dates deadlines')
        for source_url in source_urls:
            host = urlsplit(source_url).hostname
            if host and short_name:
                queries.append(f'site:{host} "{short_name}" important dates deadline')
                break
        return tuple(dict.fromkeys(queries))

    async def _apply_active_recipe(self, page: FetchedPage, text: str) -> str:
        if self.recipes is None:
            return text
        stored = await self.recipes.active_for(page.url, PageKind.DETAIL)
        if stored is None:
            return text
        try:
            extraction = apply_recipe(self.recipes.to_schema(stored), page.html, page.url)
        except RecipeValidationError:
            await self.recipes.mark_degraded(stored.id, "RECIPE_VALIDATION_FAILED")
            return text
        if not (extraction.fields or extraction.links or extraction.regex_matches):
            await self.recipes.mark_degraded(stored.id, "RECIPE_EMPTY")
            return text
        structured = extraction.model_dump_json(exclude_defaults=True)
        return f"{text}\n\nСТРУКТУРИРОВАННЫЕ ДАННЫЕ РЕЦЕПТА\n{structured}"

    async def _record_unchanged(
        self,
        user_id: int,
        conference: Conference,
        source_url: str,
    ) -> PersistenceReceipt:
        conference.last_checked_at = self.now_factory()
        await self.attempts.record_attempt(
            user_id,
            conference.id,
            MonitoringAttemptStatus.UNCHANGED,
            source_url=source_url,
        )
        subscription_id = await self.session.scalar(
            select(Subscription.id).where(
                Subscription.user_id == user_id,
                Subscription.conference_id == conference.id,
                Subscription.status == ConferenceStatus.TRACKING,
            )
        )
        await self.session.commit()
        return PersistenceReceipt(
            conference_id=conference.id,
            conference_name=conference.name,
            committed=True,
            created=False,
            tracked=True,
            subscription_id=subscription_id,
            created_fields=(),
            updated_fields=(),
            deadlines=(),
            evidence_ids=(),
            change_ids=(),
        )

    def _is_due(self, last_checked_at: datetime | None) -> bool:
        if last_checked_at is None:
            return True
        if last_checked_at.tzinfo is None:
            last_checked_at = last_checked_at.replace(tzinfo=UTC)
        return last_checked_at <= self.now_factory() - self.monitoring_interval

    async def _consecutive_failures(self, user_id: int, conference_id: int) -> int:
        statuses = list(
            await self.session.scalars(
                select(MonitoringAttempt.status)
                .where(
                    MonitoringAttempt.user_id == user_id,
                    MonitoringAttempt.conference_id == conference_id,
                )
                .order_by(MonitoringAttempt.observed_at.desc(), MonitoringAttempt.id.desc())
                .limit(20)
            )
        )
        count = 0
        for status in statuses:
            if status != MonitoringAttemptStatus.FAILED:
                break
            count += 1
        return count

    @staticmethod
    def _error_code(error: Exception) -> str:
        if isinstance(error, FetchError):
            return error.code
        return type(error).__name__

    @staticmethod
    def _has_claims(research: VerifiedConferenceResearch) -> bool:
        return bool(research.facts or research.deadlines)

    @staticmethod
    def _has_temporal_data(research: VerifiedConferenceResearch) -> bool:
        return bool(research.deadlines) or any(
            claim.kind in {"start_date", "end_date"} for claim in research.facts
        )

    @staticmethod
    def _has_deadlines(research: VerifiedConferenceResearch) -> bool:
        return bool(research.deadlines)

    @staticmethod
    def _has_event_dates(research: VerifiedConferenceResearch) -> bool:
        return any(claim.kind in {"start_date", "end_date"} for claim in research.facts)

    @staticmethod
    def _merge_research(
        first: VerifiedConferenceResearch | None,
        second: VerifiedConferenceResearch,
    ) -> VerifiedConferenceResearch:
        if first is None:
            return second

        def unique_claims(values):
            result = []
            seen: set[str] = set()
            for value in values:
                key = value.model_dump_json()
                if key in seen:
                    continue
                seen.add(key)
                result.append(value)
            return tuple(result)

        payload = second.model_dump(mode="python")
        payload["aliases"] = tuple(dict.fromkeys((*first.aliases, *second.aliases)))
        payload["facts"] = unique_claims((*first.facts, *second.facts))
        payload["deadlines"] = unique_claims((*first.deadlines, *second.deadlines))
        payload["verifier_notes"] = tuple(
            dict.fromkeys((*first.verifier_notes, *second.verifier_notes))
        )
        return VerifiedConferenceResearch.model_validate(payload)

    async def _deadline_changes(self, receipt: PersistenceReceipt) -> list[DeadlineChange]:
        if not receipt.change_ids:
            return []
        rows = list(
            await self.session.scalars(
                select(ConferenceChange).where(
                    ConferenceChange.id.in_(receipt.change_ids),
                    ConferenceChange.field_name.like("deadline:%"),
                )
            )
        )
        result: list[DeadlineChange] = []
        for row in rows:
            if not row.old_value or not row.new_value:
                continue
            try:
                old_date = date.fromisoformat(row.old_value)
                new_date = date.fromisoformat(row.new_value)
                deadline_type = DeadlineType(row.field_name.split(":", 1)[1])
            except ValueError:
                continue
            result.append(
                DeadlineChange(
                    receipt.conference_id,
                    receipt.conference_name,
                    deadline_type,
                    old_date,
                    new_date,
                    row.source_url,
                )
            )
        return result
