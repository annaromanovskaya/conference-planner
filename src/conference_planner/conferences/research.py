import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from typing import ClassVar, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select, update, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.conferences.page_urls import is_document_url
from conference_planner.ai.gateway import ModelGatewayError
from conference_planner.ai.service import AIResponseError
from conference_planner.conferences.candidate_policy import (
    TopicRelevanceContext,
    clearly_not_conference,
    has_scientific_event_evidence,
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
from conference_planner.conferences.temporal import (
    enrich_catalog_events,
    extract_temporal_research,
    merge_temporal_research,
)
from conference_planner.db.models import EventCandidate, DiscoverySearchAudit, DiscoveryURLTask, TrustedSource
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
    SourceType,
)
from conference_planner.schemas.research import (
    ConferenceCatalogResearch,
    ConferenceResearch,
    DeadlineClaim,
    FactClaim,
    VerifiedConferenceResearch,
)
from conference_planner.schemas.topics import RelevanceAssessment
from conference_planner.search.provider import SearchPage, SearchProvider
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
    search_attempts: int = 0
    error_code: str | None = None
    unresponsive_engines: tuple[str, ...] = ()


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

    async def select_research_links(
        self,
        expected_name: str,
        page_url: str,
        links: tuple[PageLink, ...],
    ) -> tuple[str, ...]: ...


class ConferenceResearchService:
    MAX_PAGE_RESULTS = 50
    _ENGLISH_QUERY_TERMS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("астрохим", "astrochemistry"),
        ("звездообраз", "star formation"),
        ("молекулярн", "molecular spectroscopy"),
        ("межзвезд", "interstellar medium"),
        ("межзвёзд", "interstellar medium"),
        ("радиоинтерфер", "radio interferometry"),
        ("машинн", "machine learning"),
        ("спектроскоп", "spectroscopy"),
        ("астроном", "astronomy"),
        ("европ", "Europe"),
        ("росси", "Russia"),
        ("ази", "Asia"),
    )

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
        discovery_job_id: int | None = None,
    ) -> None:
        self.session = session
        self.discovery_job_id = discovery_job_id
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
        self.last_search_unresponsive_engines: tuple[str, ...] = ()

    async def search(
        self,
        user_id: int,
        query: str,
        limit: int = 10,
        *,
        page: int = 1,
        search_query: str | None = None,
    ) -> list[EventCandidate]:
        bounded_limit = max(1, min(limit, self.MAX_PAGE_RESULTS))
        topics = await self._topic_contexts(user_id)
        effective_query = search_query or self._dated_search_query(query)
        try:
            search_page = await self._search_page(effective_query, page=page)
        except Exception as exc:
            if self.discovery_job_id is not None:
                self.session.add(DiscoverySearchAudit(
                    job_id=self.discovery_job_id, query=effective_query, page=page,
                    result_count=0, retained_count=0, ignored_count=0,
                    status="SEARCH_ERROR", diagnostics=[type(exc).__name__], urls=[],
                ))
                await self.session.commit()
            raise
        raw_results = list(search_page.results[:bounded_limit])
        self.last_search_result_count = len(raw_results)
        self.last_search_unresponsive_engines = search_page.unresponsive_engines
        drafts: list[CandidateDraft] = []
        inspected_results: list[dict] = []
        seen_identities: set[tuple[str, str]] = set()
        for position, result in enumerate(raw_results):
            url = str(result.url)
            normalized_url = canonical_url(url) or url
            identity = (normalized_url, normalize_name(result.title))
            if identity in seen_identities:
                inspected_results.append({"url": url, "decision": "DUPLICATE"})
                continue
            if clearly_not_conference(result.title, result.snippet):
                inspected_results.append({"url": url, "decision": "NON_CONFERENCE"})
                continue
            if not has_scientific_event_evidence(result.title, result.snippet, url):
                inspected_results.append({"url": url, "decision": "NO_EVENT_EVIDENCE"})
                continue
            keyword_relevance = evaluate_keyword_relevance(result.title, result.snippet, topics)
            # Relevance of an explicitly named scientific meeting must be checked
            # on its official page rather than inferred from the short snippet.
            official_meeting = bool(re.search(
                r"(?:^|\.)iau\.org$|(?:^|\.)eso\.org$|(?:^|\.)aas\.org$",
                urlsplit(url).hostname or "", re.I
            )) and has_scientific_event_evidence(result.title, result.snippet, url)
            # A snippet does not contain the conference programme. A zero keyword
            # overlap is not proof of topical irrelevance (e.g. AISTATS or a
            # cross-disciplinary scientific meeting). Defer the final verdict
            # until the actual website/programme has been inspected.
            inspected_results.append({
                "url": url,
                "decision": (
                    "TOPIC_REVIEW_REQUIRED"
                    if keyword_relevance.explicitly_irrelevant and not official_meeting
                    else "UNVERIFIED_CANDIDATE"
                ),
            })
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
        if self.discovery_job_id is not None:
            self.session.add(DiscoverySearchAudit(
                job_id=self.discovery_job_id,
                query=effective_query, page=page,
                result_count=len(raw_results), retained_count=len(drafts),
                ignored_count=len(raw_results) - len(drafts),
                status="EMPTY" if not raw_results else "RESULTS_RECEIVED",
                diagnostics=list(search_page.unresponsive_engines),
                urls=inspected_results,
            ))
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

    async def search_trusted_sources(
        self,
        user_id: int,
        query: str,
        *,
        max_sources: int | None = None,
        max_links_per_source: int | None = None,
        max_depth: int = 4,
        max_pages: int = 200,
    ) -> dict:
        """Bounded, independent crawl of enabled user sources, without SearXNG.

        Counts actual fetched pages and records each URL and decision. Extracted
        URLs remain UNVERIFIABLE until inspect() validates source evidence.
        """
        source_query = (select(TrustedSource).where(TrustedSource.enabled.is_(True))
                        .order_by(TrustedSource.id))
        if max_sources is not None:
            source_query = source_query.limit(max_sources)
        sources = list(await self.session.scalars(source_query))
        years = set(re.findall(r"\b20\d{2}\b", query))
        seen: set[str] = set()
        queued: set[str] = set()
        queue: deque[tuple[str, int, str, str]] = deque()
        if self.discovery_job_id is not None:
            # Seed idempotently. Existing DONE records are not reprocessed on restart.
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            for source in sources:
                if source.source_type in {SourceType.PAGE, SourceType.DOMAIN}:
                    key = canonical_url(source.url) or source.url
                    await self.session.execute(sqlite_insert(DiscoveryURLTask).values(
                        job_id=self.discovery_job_id, url=source.url, normalized_url=key,
                        root_url=source.url, label=source.name, depth=0, priority=0,
                        task_type="CATALOG", status="PENDING", attempts=0,
                        created_at=datetime.now(UTC).replace(tzinfo=None),
                        updated_at=datetime.now(UTC).replace(tzinfo=None),
                    ).on_conflict_do_nothing(index_elements=["job_id", "normalized_url", "task_type"]))
            await self.session.commit()
        else:
            for source in sources:
                if source.source_type in {SourceType.PAGE, SourceType.DOMAIN}:
                    queue.append((source.url, 0, source.url, source.name))
                    queued.add(canonical_url(source.url) or source.url)
        added_ids: list[int] = []
        active_ids: list[int] = []
        errors: list[str] = []
        opened = 0
        inspected_candidates: set[str] = set()
        while opened < max_pages:
            task = None
            if self.discovery_job_id is not None:
                now = datetime.now(UTC).replace(tzinfo=None)
                task = await self.session.scalar(select(DiscoveryURLTask).where(
                    DiscoveryURLTask.job_id == self.discovery_job_id,
                    DiscoveryURLTask.task_type == "CATALOG",
                    DiscoveryURLTask.attempts < 3,
                    or_(
                        (DiscoveryURLTask.status == "PENDING") & (
                            (DiscoveryURLTask.next_retry_at.is_(None)) |
                            (DiscoveryURLTask.next_retry_at <= now)
                        ),
                        (DiscoveryURLTask.status == "RUNNING") & (
                            (DiscoveryURLTask.lease_until.is_(None)) |
                            (DiscoveryURLTask.lease_until < now)
                        ),
                    ),
                ).order_by(DiscoveryURLTask.attempts, DiscoveryURLTask.depth, DiscoveryURLTask.priority, DiscoveryURLTask.id).limit(1))
                if task is None:
                    break
                if is_document_url(task.url):
                    task.status = "SKIPPED"
                    task.error_code = "NON_HTML_DOCUMENT"
                    task.lease_until = None
                    await self.session.commit()
                    continue
                task.status = "RUNNING"
                task.attempts += 1
                fetch_timeout = max(10, int(os.getenv("DISCOVERY_CATALOG_TIMEOUT_SECONDS", "60")))
                task.lease_until = (datetime.now(UTC) + timedelta(seconds=fetch_timeout + 60)).replace(tzinfo=None)
                task.next_retry_at = None
                await self.session.commit()
                task_id = task.id
                url, depth, root, label = task.url, task.depth, task.root_url, task.label
            else:
                if not queue:
                    break
                url, depth, root, label = queue.popleft()
            if is_document_url(url):
                # Non-HTML source URLs must not reach the page fetcher.
                if task is not None:
                    task.status = "SKIPPED"
                    task.error_code = "NON_HTML_DOCUMENT"
                    task.lease_until = None
                    await self.session.commit()
                continue
            key = canonical_url(url) or url
            if key in seen:
                continue
            seen.add(key)
            log_urls = [{"url": url, "decision": "FETCH_ATTEMPT", "depth": depth}]
            try:
                async with asyncio.timeout(max(10, int(os.getenv("DISCOVERY_CATALOG_TIMEOUT_SECONDS", "60")))):
                    page = await self.fetcher.fetch(url, max_bytes=self.max_page_bytes)
                opened += 1
                page_text = extract_page_text(page.html, max_chars=self.max_page_chars)
                links = extract_page_links(page.html, page.url, max_links=2000)
                log_urls[0]["decision"] = "SOURCE_OPENED" if depth == 0 else "CATALOG_OPENED"
                # A catalog is recognised from both URL and anchor semantics.
                catalog_links = 0
                drafts: list[CandidateDraft] = []
                for link in links:
                    target = canonical_url(link.url) or link.url
                    if target in seen or target == key:
                        continue
                    text = f"{link.text} {link.url}"
                    # HTML crawler cannot parse PDF/Word. Do not queue these as pages.
                    if is_document_url(link.url):
                        log_urls.append({"url": link.url, "decision": "DOCUMENT_SKIPPED"})
                        continue
                    if clearly_not_conference(link.text, text):
                        continue
                    # Reject explicitly outdated editions; year-less annual
                    # index links are still eligible for further exploration.
                    mentioned = set(re.findall(r"(?<!\d)20\d{2}(?!\d)", text))
                    if years and mentioned and not (mentioned & years):
                        log_urls.append({"url": link.url, "decision": "OTHER_YEAR"})
                        continue
                    event = has_scientific_event_evidence(link.text, text, link.url)
                    if not event:
                        continue
                    # Pages looking like directories get an additional crawl
                    # step; a dedicated conference page may also be checked
                    # through inspect(), without trusting its title alone.
                    directory = bool(re.search(
                        r"(?:/meetings/?$|/conference/?$|/conferences/?$|"
                        r"/events/?$|/meetings/(?:recent|this-month|garching)/?$|"
                        r"/conference/(?:index|annconf)/?)", link.url, re.I
                    ))
                    if directory and depth < max_depth and target not in queued:
                        if self.discovery_job_id is not None:
                            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                            await self.session.execute(sqlite_insert(DiscoveryURLTask).values(
                                job_id=self.discovery_job_id, url=link.url, normalized_url=target,
                                parent_url=page.url, root_url=root, label=label,
                                task_type="CATALOG", status="PENDING", depth=depth + 1,
                                priority=0, attempts=0,
                                created_at=datetime.now(UTC).replace(tzinfo=None),
                                updated_at=datetime.now(UTC).replace(tzinfo=None),
                            ).on_conflict_do_nothing(index_elements=["job_id", "normalized_url", "task_type"]))
                        else:
                            queue.append((link.url, depth + 1, root, label))
                        queued.add(target)
                        catalog_links += 1
                        log_urls.append({"url": link.url, "decision": "CATALOG_QUEUED"})
                    elif (max_links_per_source is None or len(drafts) < max_links_per_source) and target not in inspected_candidates:
                        inspected_candidates.add(target)
                        drafts.append(CandidateDraft(
                            title=link.text[:500], source_url=link.url,
                            query=query, relevance_score=0.5,
                            summary=f"Linked from trusted source: {page.url}; anchor: {link.text[:300]}",
                            verification_status=VerificationStatus.UNVERIFIABLE,
                        ))
                        log_urls.append({"url": link.url, "decision": "UNVERIFIED_CANDIDATE"})
                existing = set(await self.session.scalars(
                    select(EventCandidate.source_url).where(
                        EventCandidate.user_id == user_id,
                        EventCandidate.source_url.in_([d.source_url for d in drafts]),
                    )
                )) if drafts else set()
                stored = await self.repository.create_candidates(
                    user_id, drafts, expires_at=datetime.now(UTC) + self.candidate_ttl,
                )
                active_ids.extend(c.id for c in stored
                                  if c.status == CandidateStatus.ACTIVE and c.id not in active_ids)
                added_ids.extend(c.id for c in stored
                                 if c.status == CandidateStatus.ACTIVE
                                 and c.source_url not in existing and c.id not in added_ids)
                if self.discovery_job_id is not None:
                    self.session.add(DiscoverySearchAudit(
                        job_id=self.discovery_job_id,
                        query=f"[trusted source] {page.url}", page=depth + 1,
                        result_count=len(links), retained_count=len(drafts),
                        ignored_count=max(0, len(links) - len(drafts) - catalog_links),
                        status="SOURCE_OPENED" if depth == 0 else "CATALOG_OPENED",
                        diagnostics=[], urls=log_urls,
                    ))
                if task is not None:
                    task.status, task.lease_until, task.error_code = "DONE", None, None
                await self.session.commit()
            except Exception as exc:
                await self.session.rollback()
                if task is not None:
                    task = await self.session.get(DiscoveryURLTask, task_id)
                    task.status = "PENDING" if task.attempts < 3 else "FAILED"
                    task.lease_until = None
                    task.next_retry_at = (
                        datetime.now(UTC) + timedelta(seconds=30 * 3 ** (task.attempts - 1))
                    ).replace(tzinfo=None) if task.status == "PENDING" else None
                    task.error_code = type(exc).__name__
                errors.append(f"{url}: {type(exc).__name__}")
                if self.discovery_job_id is not None:
                    self.session.add(DiscoverySearchAudit(
                        job_id=self.discovery_job_id,
                        query=f"[trusted source] {url}", page=depth + 1,
                        result_count=0, retained_count=0, ignored_count=0,
                        status="SOURCE_ERROR", diagnostics=[type(exc).__name__],
                        urls=[{"url": url, "decision": "SOURCE_UNAVAILABLE", "depth": depth}],
                    ))
                    await self.session.commit()
        if self.discovery_job_id is not None:
            pending = await self.session.scalar(select(func.count()).select_from(DiscoveryURLTask).where(
                DiscoveryURLTask.job_id == self.discovery_job_id,
                DiscoveryURLTask.status.in_(["PENDING", "RUNNING"])))
        else:
            pending = len(queue)
        return {"new_ids": tuple(added_ids), "active_ids": tuple(active_ids),
                "sources_opened": opened,
                "sources_total": len(sources), "errors": tuple(errors),
                "pending_catalogs": pending}

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
        diagnostics: list[str] = []
        searches_issued = 0

        async def attempt(page: int, search_query: str | None = None) -> None:
            nonlocal duplicates, pages_checked, searches_issued
            if searches_issued:
                await asyncio.sleep(1.5)  # Avoid bursts against upstream engines.
            searches_issued += 1
            candidates = await self.search(
                user_id,
                query,
                limit=bounded_results,
                page=page,
                search_query=search_query,
            )
            pages_checked += 1
            for diagnostic in self.last_search_unresponsive_engines:
                if diagnostic not in diagnostics:
                    diagnostics.append(diagnostic)
            for candidate in candidates:
                if candidate.id not in active_ids:
                    active_ids.append(candidate.id)
            for candidate_id in self.last_search_new_ids:
                if candidate_id not in new_ids:
                    new_ids.append(candidate_id)
            duplicates += self.last_search_duplicate_count

        generic_topics = bool(re.search(
            r"моим\s+тем|по\s+моим\s+тем|my\s+topics", query.casefold()
        ))
        if not generic_topics:
            await attempt(1)
        if not generic_topics and self.last_search_result_count == 0:
            await self._wait_before_search_retry(1.0)
            english_query = await self._english_search_query(query)
            await attempt(1, english_query)
            if self.last_search_result_count == 0:
                await self._wait_before_search_retry(2.0)
                await attempt(2, english_query)
            exhausted = self.last_search_result_count == 0
            if exhausted and bounded_pages > 1:
                year = " ".join(dict.fromkeys(re.findall(r"\b20\d{2}\b", query)))
                for topic_term in ("stellar spectroscopy", "astrophysics", "astronomy"):
                    await attempt(1, f"{topic_term} conference {year} abstract submission".strip())
                    if self.last_search_result_count:
                        exhausted = False
                        break
        elif not generic_topics and not new_ids:
            for page in range(2, bounded_pages + 1):
                await attempt(page)
                if new_ids or self.last_search_result_count == 0:
                    exhausted = self.last_search_result_count == 0
                    break

        # A generic "find conferences relevant to my topics" is not a useful
        # web query. Expand it to the owner's actual enabled research topics.
        # Search each topic with its own query; keep strict budget bounds.
        if generic_topics:
            topics = await self._topic_contexts(user_id)
            year = " ".join(dict.fromkeys(re.findall(r"\b20\d{2}\b", query)))
            # Use stable short domain phrases; an LLM translation may turn
            # 'high-resolution' into consumer electronics or 'chemical' into
            # a dictionary lookup when search engines are degraded.
            searched: set[str] = set()
            for topic in topics[:8]:
                title = topic.name.strip()
                if not title:
                    continue
                key = self._scientific_search_topic(title)
                search_text = f'"{key}" conference {year}'.strip()
                if search_text in searched:
                    continue
                searched.add(search_text)
                await attempt(1, search_text)
            exhausted = not active_ids and not new_ids

        return CandidateSearchReport(
            active_ids=tuple(active_ids),
            new_ids=tuple(new_ids),
            pages_checked=pages_checked,
            duplicates=duplicates,
            exhausted=exhausted,
            search_attempts=pages_checked,
            error_code=(
                "SEARCH_UNAVAILABLE"
                if exhausted and diagnostics
                else "SEARCH_EMPTY" if exhausted else
                "NO_RELEVANT_RESULTS" if not active_ids else None
            ),
            unresponsive_engines=tuple(diagnostics),
        )

    @staticmethod
    def _scientific_search_topic(value: str) -> str:
        lower = value.casefold()
        translations = (
            ("спектроскоп", "stellar spectroscopy"),
            ("химическ", "stellar chemical abundances"),
            ("пекуляр", "chemically peculiar stars"),
            ("двойн", "spectroscopic binary stars"),
            ("магнит", "magnetic stars"),
            ("машинн", "machine learning astronomy"),
            ("атмосфер", "stellar atmospheres"),
            ("астрофиз", "astrophysics"),
            ("астрохим", "astrochemistry"),
            ("звездообраз", "star formation"),
        )
        for fragment, label in translations:
            if fragment in lower:
                return label
        # Preserve scientific phrases from user settings; avoid broad keywords.
        stripped = re.sub(r"[^a-zA-Z0-9\s-]", " ", value)
        return " ".join(stripped.split())[:75] or "astronomy"

    async def _wait_before_search_retry(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _search_page(self, query: str, *, page: int) -> SearchPage:
        search_page = getattr(self.search_provider, "search_page", None)
        if callable(search_page):
            return await search_page(query, page=page)
        results = await self.search_provider.search(query, page=page)
        return SearchPage(tuple(results))

    def _dated_search_query(self, query: str, *, english: bool = False) -> str:
        """Use search keywords only; dates belong to extraction/verification."""
        return " ".join(query.split())[:240]

    async def _english_search_query(self, query: str) -> str:
        translator = getattr(self.ai, "translate_search_query", None)
        try:
            translated = await translator(query) if callable(translator) else ""
        except Exception:
            translated = ""
        if (
            not isinstance(translated, str)
            or not translated.strip()
            or re.search(r"[А-Яа-яЁё]", translated)
        ):
            translated = self._fallback_english_search_query(query)
        return self._dated_search_query(translated.strip(), english=True)

    def _fallback_english_search_query(self, query: str) -> str:
        normalized = query.casefold()
        terms = [
            english
            for fragment, english in self._ENGLISH_QUERY_TERMS
            if fragment in normalized
        ]
        terms.extend(re.findall(r"\b20\d{2}\b", query))
        if not any(term in {"astronomy", "astrochemistry"} for term in terms):
            terms.append("astronomy")
        terms.extend(
            (
                "scientific conference",
                "official website",
                "abstract submission deadline",
            )
        )
        return " ".join(dict.fromkeys(terms))

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
        if clearly_not_conference(candidate.title, candidate.summary):
            await self.repository.transition_candidate(
                user_id, candidate_id,
                CandidateStatus.REJECTED,
                CandidateDecisionReason.LOW_RELEVANCE,
                CandidateDecisionSource.SYSTEM,
                self._now(),
            )
            await self.session.commit()
            raise CandidateRejectedError("search result is clearly not a conference")
        await self._report(progress, f"Открываю сайт: {candidate.source_url}")
        page, page_text, page_links = await self._fetch_candidate_page(candidate)
        primary_documents = (EvidenceDocument(page.url, page_text),)
        deterministic = self._deterministic_research(candidate.title, primary_documents)
        dedicated_page = self._is_dedicated_event_page(
            candidate.title,
            page_text,
            deterministic,
        )
        catalog_text = self._candidate_evidence_text(candidate, page_text)
        catalog = (
            None
            if dedicated_page
            else await self._extract_catalog(page.url, catalog_text)
        )
        if dedicated_page:
            await self._report(
                progress,
                "Обычный парсер распознал отдельную страницу конференции.",
            )
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
        preview = await self.repository.load_candidate_snapshot(
            candidate.user_id,
            candidate.id,
        )
        if catalog is None and (
            self._is_generic_title(candidate.title) or preview is not None
        ):
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
        text = self._candidate_evidence_text(
            candidate,
            self._documents_text(documents),
        )
        deterministic = self._deterministic_research(candidate.title, documents)
        await self._report(
            progress,
            "Обычный парсер: "
            f"найдено дат {len(deterministic.facts)}, "
            f"дедлайнов {len(deterministic.deadlines)}.",
        )
        if catalog is not None and catalog.events:
            extracted, score = self._best_catalog_event(candidate.title, catalog.events)
            if self._is_generic_title(candidate.title) or (
                len(catalog.events) > 1 and score < 0.45
            ):
                raise CandidateUnusableError("catalog candidate must be inspected first")
            extracted = merge_temporal_research(extracted, deterministic)
            if len(documents) > 1 and not self._deterministic_is_sufficient(
                deterministic, documents
            ):
                await self._report(progress, "Модель разбирает нестандартную страницу.")
                try:
                    detailed = await self.ai.extract_conference_research(
                        page.url, text, candidate.title
                    )
                except (AIResponseError, ModelGatewayError):
                    if not self._is_usable_event(extracted):
                        raise
                    fetch_notes = (
                        *fetch_notes,
                        "Модельный разбор недоступен; сохранены данные обычного парсера.",
                    )
                else:
                    extracted = self._merge_research(extracted, detailed)
                    extracted = merge_temporal_research(extracted, deterministic)
        elif self._deterministic_is_sufficient(deterministic, documents):
            extracted = self._as_secondary_research(deterministic)
        else:
            await self._report(progress, "Модель разбирает нестандартную страницу.")
            try:
                model_research = await ResearchRole(self.ai).extract(
                    (EvidenceDocument(page.url, text),), candidate.title
                )
            except (AIResponseError, ModelGatewayError):
                if not self._is_usable_event(deterministic):
                    raise
                extracted = deterministic
                fetch_notes = (
                    *fetch_notes,
                    "Модельный разбор недоступен; сохранены данные обычного парсера.",
                )
            else:
                extracted = merge_temporal_research(model_research, deterministic)
        researcher_model = self._last_model_label()
        if researcher_model is not None:
            await self._report(
                progress,
                f"Модель исследователя: {researcher_model}.",
            )
        extracted = self._attribute_claim_sources(extracted, documents)
        validator = EvidenceValidator()
        deterministic_identities = {
            self._claim_identity(claim)
            for claim in (*deterministic.facts, *deterministic.deadlines)
        }
        needs_verification = any(
            verification_required(claim, validator.context_for(claim, documents))
            for claim in (*extracted.facts, *extracted.deadlines)
            if self._claim_identity(claim) not in deterministic_identities
        )
        if needs_verification:
            verified = await VerifierRole(self.ai).verify(extracted, documents)
            verifier_model = self._last_model_label(verifier=True)
            if verifier_model is not None:
                await self._report(
                    progress,
                    f"Модель проверяющего: {verifier_model}.",
                )
        else:
            verified = VerifiedConferenceResearch.model_validate(
                extracted.model_dump(mode="python")
            )
        verified = self._recover_visible_event_dates(
            verified,
            self._event_specific_text(
                candidate.title,
                self._fetched_page_only(documents[0].text),
            ),
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
        programme_sources = tuple(
            (str(doc.url), doc.text[:3500])
            for doc in documents
            if self._has_scientific_programme_evidence((doc,))
        )[:6]
        relevance = (
            await self._rank_with_programme(verified, topics, programme_sources)
            if topics
            else RelevanceAssessment(score=1.0, explanation="Темы не настроены")
        )
        candidate.topic_relevance_score = relevance.score
        candidate.topic_relevance_reason = relevance.explanation
        # Do not irreversibly mark a conference as low-relevance when the
        # scientific programme is not published or was not accessible. The
        # title alone cannot support a negative topical decision.
        programme_evidence = self._has_scientific_programme_evidence(documents)
        if relevance.score < self.min_relevance_score and not programme_evidence:
            candidate.topic_relevance_reason = (
                "Предварительная оценка; программа/секции/доклады не найдены. "
                + relevance.explanation
            )[:1000]
        elif relevance.score < self.min_relevance_score:
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

    async def _rank_with_programme(
        self,
        research: VerifiedConferenceResearch,
        topics: tuple[TopicRelevanceContext, ...],
        programme_sources: tuple[tuple[str, str], ...],
    ) -> RelevanceAssessment:
        """Prefer evidence-backed programme scoring; preserve mock/provider compatibility."""
        scorer = getattr(self.ai, "rank_research_programme_relevance", None)
        if callable(scorer) and programme_sources:
            return await scorer(research, topics, programme_sources)
        return await self.ai.rank_research_relevance(research, topics)

    @staticmethod
    def _has_scientific_programme_evidence(documents: tuple[EvidenceDocument, ...]) -> bool:
        """Require actual programme material, not a URL or a registration heading."""
        marker = re.compile(
            r"\b(?:scientific program(?:me)?|scientific sessions?|session topics?|"
            r"contributed talks?|oral presentations?|invited talks?|poster sessions?|"
            r"list of abstracts|abstract book|conference programme|"
            r"научн\w* программ\w*|секци\w*|темы докладов|приглаш[её]нн\w* доклад\w*)\b",
            re.I,
        )
        # A title/URL naming 'program' is insufficient: require the material in
        # extracted page content, which contains the actual topic descriptions.
        return any(marker.search(doc.text) for doc in documents)

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
        safe_links = self._safe_research_links(page.url, page_links)
        ranked_links = self._relevant_research_links(candidate.title, page.url, page_links)
        program_marker = re.compile(
            r"(?:program(?:me)?|scientific[ -]?topics?|sessions?|abstracts?|"
            r"contributed[ -]?talks?|oral[ -]?presentations?|poster(?:s)?|"
            r"speakers?|timetable|schedule|proceedings|"
            r"программ[аыуе]|секци[яиий]|тематик[аи]|доклад[ыов]|тезис[ыов])",
            re.I,
        )
        # Content of the actual scientific programme is more informative than
        # the title of an event or an unrelated generic page.
        ranked_links = tuple(sorted(
            ranked_links,
            key=lambda link: (
                -bool(program_marker.search(f"{link.text} {link.url}")),
                -self._research_link_score(candidate.title, link),
            ),
        ))
        queue = [
            (link, 1) for link in ranked_links
            if program_marker.search(f"{link.text} {link.url}")
            or self._research_link_score(candidate.title, link) > 0
        ]
        reserve = next(
            (
                (link, 1)
                for link in safe_links
                if self._research_link_score(candidate.title, link) <= 0
            ),
            None,
        )

        async def fetch_link(link: PageLink, depth: int) -> tuple[PageLink, ...]:
            normalized_url = link.url.rstrip("/")
            if normalized_url in visited or is_document_url(link.url):
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

        while queue and len(documents) < 18:
            link, depth = queue.pop(0)
            children = await fetch_link(link, depth)
            queue.extend((child, depth + 1) for child in children)

        if len(documents) < 18 and (
            not self._documents_have_usable_temporal_research(candidate.title, documents)
            or not self._has_scientific_programme_evidence(tuple(documents))
        ):
            selector = getattr(self.ai, "select_research_links", None)
            if callable(selector) and safe_links:
                await self._report(
                    progress,
                    "Модель выбирает связанные страницы с датами и дедлайнами.",
                )
                try:
                    selected_urls = await selector(candidate.title, page.url, safe_links)
                except (AIResponseError, ModelGatewayError):
                    notes.append("Модель не смогла выбрать связанные страницы.")
                else:
                    allowed = {link.url.rstrip("/"): link for link in safe_links}
                    page_host = urlsplit(page.url).netloc.casefold()
                    for selected_url in selected_urls[:4]:
                        normalized = str(selected_url).rstrip("/")
                        selected = allowed.get(normalized)
                        if (
                            selected is None
                            or urlsplit(selected.url).scheme not in {"http", "https"}
                            or urlsplit(selected.url).netloc.casefold() != page_host
                            or normalized in visited
                            or len(documents) >= 18
                        ):
                            continue
                        children = await fetch_link(selected, 1)
                        queue.extend((child, 2) for child in children)
                    while queue and len(documents) < 18:
                        link, depth = queue.pop(0)
                        await fetch_link(link, depth)
        if (
            reserve is not None
            and len(documents) < 18
            and not self._documents_have_usable_temporal_research(candidate.title, documents)
        ):
            await fetch_link(*reserve)
        return tuple(documents), tuple(notes)

    @staticmethod
    def _safe_research_links(
        page_url: str,
        links: tuple[PageLink, ...],
    ) -> tuple[PageLink, ...]:
        page_host = urlsplit(page_url).netloc.casefold()
        selected: list[PageLink] = []
        seen: set[str] = set()
        for link in links:
            normalized = link.url.rstrip("/")
            parsed = urlsplit(link.url)
            if (
                normalized == page_url.rstrip("/")
                or normalized in seen
                or parsed.scheme not in {"http", "https"}
                or parsed.netloc.casefold() != page_host
                or is_document_url(link.url)
                or parsed.path.casefold().endswith(
                    (".pdf", ".jpg", ".jpeg", ".png", ".webp")
                )
            ):
                continue
            seen.add(normalized)
            selected.append(link)
            if len(selected) >= 100:
                break
        return tuple(selected)

    @classmethod
    def _deterministic_research(
        cls,
        expected_name: str,
        documents: tuple[EvidenceDocument, ...] | list[EvidenceDocument],
    ) -> ConferenceResearch:
        first = documents[0]
        first_text = cls._event_specific_text(
            expected_name,
            cls._fetched_page_only(first.text),
        )
        combined = extract_temporal_research(first.url, first_text, expected_name)
        for document in documents[1:]:
            current = extract_temporal_research(
                document.url,
                cls._fetched_page_only(document.text),
                expected_name,
            )
            combined = merge_temporal_research(combined, current)
        return combined

    @staticmethod
    def _event_specific_text(expected_name: str, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return text
        expected = normalize_name(expected_name)
        scored = [
            (SequenceMatcher(None, expected, normalize_name(line)).ratio(), index)
            for index, line in enumerate(lines)
        ]
        score, anchor = max(scored, default=(0.0, 0))
        if score < 0.55:
            return text
        return "\n".join(lines[anchor : anchor + 30])

    @classmethod
    def _is_dedicated_event_page(
        cls,
        expected_name: str,
        text: str,
        deterministic: ConferenceResearch,
    ) -> bool:
        if cls._is_generic_title(expected_name) or not cls._deterministic_is_sufficient(
            deterministic,
            (EvidenceDocument("https://local.invalid/", text),),
        ):
            return False
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        expected = normalize_name(expected_name)
        anchors = [
            index
            for index, line in enumerate(lines[:5])
            if SequenceMatcher(None, expected, normalize_name(line)).ratio() >= 0.55
        ]
        return bool(anchors) and min(anchors) <= 2

    @staticmethod
    def _fetched_page_only(text: str) -> str:
        if "FETCHED_PAGE_TEXT:\n" not in text:
            return text
        return text.rsplit("FETCHED_PAGE_TEXT:\n", maxsplit=1)[-1]

    @classmethod
    def _deterministic_is_sufficient(
        cls,
        research: ConferenceResearch,
        documents: tuple[EvidenceDocument, ...] | list[EvidenceDocument] = (),
    ) -> bool:
        has_start = any(claim.kind == "start_date" for claim in research.facts)
        if not has_start or not research.deadlines:
            return False
        claimed_lines = {
            " ".join(claim.excerpt.casefold().split()) for claim in research.deadlines
        }
        for document in documents:
            for raw_line in cls._fetched_page_only(document.text).splitlines():
                line = " ".join(raw_line.casefold().split())
                if not re.search(r"\b20\d{2}\b", line):
                    continue
                if not any(
                    marker in line
                    for marker in (
                        "deadline",
                        "registration",
                        "submission",
                        "abstract",
                        "дедлайн",
                        "регистрац",
                        "подач",
                        "тезис",
                    )
                ):
                    continue
                if not any(excerpt in line or line in excerpt for excerpt in claimed_lines):
                    return False
        return True

    @staticmethod
    def _as_secondary_research(research: ConferenceResearch) -> ConferenceResearch:
        def convert(claim: FactClaim | DeadlineClaim):
            status = (
                VerificationStatus.TENTATIVE
                if getattr(claim, "tentative", False)
                else VerificationStatus.SECONDARY_UNVERIFIED
            )
            return claim.model_copy(
                update={
                    "source_class": EvidenceSourceClass.SECONDARY,
                    "verification_status": status,
                }
            )

        return research.model_copy(
            update={
                "source_class": EvidenceSourceClass.SECONDARY,
                "facts": tuple(convert(claim) for claim in research.facts),
                "deadlines": tuple(convert(claim) for claim in research.deadlines),
            }
        )

    @staticmethod
    def _claim_identity(claim: FactClaim | DeadlineClaim) -> tuple[object, ...]:
        if isinstance(claim, FactClaim):
            return "fact", claim.kind, claim.value, claim.excerpt
        return (
            "deadline",
            claim.deadline_type,
            claim.exact_date,
            claim.approximate_year,
            claim.approximate_month,
            claim.excerpt,
        )

    @classmethod
    def _documents_have_usable_temporal_research(
        cls,
        expected_name: str,
        documents: list[EvidenceDocument],
    ) -> bool:
        return cls._deterministic_is_sufficient(
            cls._deterministic_research(expected_name, documents),
            documents,
        )

    @staticmethod
    async def _report(
        progress: Callable[[str], Awaitable[None]] | None,
        text: str,
    ) -> None:
        if progress is not None:
            await progress(text)

    def _last_model_label(self, *, verifier: bool = False) -> str | None:
        labeler = getattr(self.ai, "last_model_label", None)
        if not callable(labeler):
            return None
        label = labeler(verifier=verifier)
        return label if isinstance(label, str) and label else None

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
        selected = [item[2] for item in ranked[:16]]
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
            "scientific topic",
            "session",
            "speaker",
            "poster",
            "talk",
            "proceedings",
            "секци",
            "доклад",
            "программ",
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
        parsed_url = urlsplit(link.url)
        url_text = normalize_name(f"{parsed_url.path} {parsed_url.query}")
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
