"""Bounded agent research tool chain for scheduled conference cycles.

The workflow engine invokes this from SEARCH_CANDIDATES; data changes stay in the
existing research service. A failure of one tool never certifies an event.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from conference_planner.conferences.agent_tools import (
    compare_events, official_site_queries, refine_queries, summarize_deadlines,
)
from conference_planner.conferences.deduplication import CandidateIdentity
from conference_planner.conferences.research import CatalogExpansion, ConferenceResearchService
from conference_planner.db.models import Conference, TrustedSource
from conference_planner.schemas.common import CandidateStatus, SourceType


@dataclass(slots=True)
class ScheduledToolResults:
    added_ids: list[int] = field(default_factory=list)
    checked_pages: int = 0
    expanded_catalogs: int = 0
    checked_deadlines: int = 0
    potential_duplicates: int = 0
    attempts: int = 0
    errors: list[str] = field(default_factory=list)


async def run_scheduled_agent_tools(
    service: ConferenceResearchService,
    user_id: int,
    year: int,
    topic: str,
    candidate_ids: list[int],
    previous_queries: list[str],
    *,
    max_extra_queries: int = 2,
    max_verifications: int = 5,
) -> ScheduledToolResults:
    """Execute the six MCP-equivalent tool operations using shared implementations.

    Search limits and candidate limits are deliberately bounded per cycle. Candidate
    page failures are reported, never converted into verified conference claims.
    """
    out = ScheduledToolResults()
    session = service.session
    # Tools: search_official_sources (every cycle), and
    # search_strategy_refine when the original discovery was empty.
    sources = list(await session.scalars(select(TrustedSource).where(
        TrustedSource.enabled.is_(True), TrustedSource.source_type == SourceType.DOMAIN,
    ).limit(20)))
    official = official_site_queries(topic, year, [s.url for s in sources], limit=1)
    refined = (
        refine_queries(topic, year, previous_queries, limit=2)["queries"]
        if not candidate_ids else []
    )
    queries = list(dict.fromkeys([*official, *refined]))[:max_extra_queries]
    for query in queries:
        out.attempts += 1
        try:
            report = await service.search_fresh(
                user_id, query, page_limit=1, results_per_page=10,
            )
            candidate_ids.extend(x for x in report.new_ids if x not in candidate_ids)
            out.added_ids.extend(x for x in report.new_ids if x not in out.added_ids)
            if report.error_code:
                out.errors.append(f"SEARCH:{report.error_code}")
        except Exception as exc:
            await session.rollback()
            out.errors.append(f"SEARCH:{type(exc).__name__}")
    # Tools: verify_conference_page / expand_conference_catalog / verify_deadlines
    # and deduplicate_events. Inspect performs actual site retrieval and verified
    # research, including catalog expansion where appropriate.
    for candidate_id in candidate_ids[:max_verifications]:
        try:
            candidate = await service.repository.get_candidate(user_id, candidate_id)
            if candidate.status != CandidateStatus.ACTIVE:
                continue
            research = await service.inspect(user_id, candidate_id)
            out.checked_pages += 1
            if isinstance(research, CatalogExpansion):
                out.expanded_catalogs += 1
                out.added_ids.extend(x for x in research.created_candidate_ids if x not in out.added_ids)
                continue
            snapshot = await service.repository.load_candidate_snapshot(user_id, candidate_id)
            if snapshot is not None:
                deadlines = summarize_deadlines(snapshot)
                out.checked_deadlines += len(deadlines["deadlines"])
            # Preview duplicates only: persistence and merge remain user-controlled.
            persisted = list(await session.scalars(select(Conference).where(
                Conference.is_test.is_(False),
            ).limit(200)))
            current = CandidateIdentity(
                candidate.id, candidate.title, candidate.start_date, None,
                candidate.official_url or candidate.source_url,
            )
            others = [CandidateIdentity(
                x.id, x.name, x.start_date, x.acronym, x.website,
            ) for x in persisted]
            out.potential_duplicates += int(compare_events(current, others)["duplicate"])
        except Exception as exc:
            await session.rollback()
            out.errors.append(f"VERIFY:{candidate_id}:{type(exc).__name__}")
    await session.commit()
    return out
