from dataclasses import dataclass
from datetime import UTC, date, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.conferences.deduplication import normalize_name
from conference_scout.db.models import (
    ConferenceAlias,
    ConferenceChange,
    ConferenceEvidence,
    EventCandidate,
    MonitoringAttempt,
)
from conference_scout.schemas.common import (
    CandidateDecisionReason,
    CandidateDecisionSource,
    CandidateStatus,
    EvidenceSourceClass,
    MonitoringAttemptStatus,
    VerificationStatus,
)
from conference_scout.schemas.research import ConferenceResearch, VerifiedConferenceResearch
from conference_scout.schemas.topics import RelevanceAssessment


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    title: str
    source_url: str
    query: str
    relevance_score: float
    verification_status: VerificationStatus
    summary: str | None = None


class ConferenceResearchRepository:
    MAX_CANDIDATES_PER_LIST = 100
    MAX_EVIDENCE_EXCERPT = 1_000
    MAX_FACT_VALUE = 4_000

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_candidates(
        self,
        user_id: int,
        candidates: list[CandidateDraft],
        *,
        expires_at: datetime,
    ) -> list[EventCandidate]:
        result: list[EventCandidate] = []
        seen_candidates: dict[tuple[str, str], EventCandidate] = {}
        for draft in candidates:
            source_url = draft.source_url.strip()
            title = draft.title.strip()[:500]
            normalized_title = normalize_name(title)
            if not source_url or not normalized_title:
                continue
            identity = (source_url, normalized_title)
            candidate = seen_candidates.get(identity)
            first_seen = candidate is None
            if candidate is None:
                candidate = await self.session.scalar(
                    select(EventCandidate).where(
                        EventCandidate.user_id == user_id,
                        EventCandidate.source_url == source_url,
                        EventCandidate.normalized_title == normalized_title,
                    )
                )
                if candidate is not None:
                    seen_candidates[identity] = candidate
            if candidate is None:
                same_url = list(
                    await self.session.scalars(
                        select(EventCandidate).where(
                            EventCandidate.user_id == user_id,
                            EventCandidate.source_url == source_url,
                        )
                    )
                )
                compatible = [
                    item
                    for item in same_url
                    if item.normalized_title in normalized_title
                    or normalized_title in item.normalized_title
                ]
                if len(compatible) == 1:
                    candidate = compatible[0]
                    seen_candidates[identity] = candidate
            created = candidate is None
            if candidate is None:
                candidate = EventCandidate(
                    user_id=user_id,
                    source_url=source_url,
                    normalized_title=normalized_title,
                )
                self.session.add(candidate)
                candidate.status = CandidateStatus.ACTIVE
                seen_candidates[identity] = candidate
            preserve_verified = (
                not created
                and candidate.verification_status != VerificationStatus.UNVERIFIABLE
                and draft.verification_status == VerificationStatus.UNVERIFIABLE
            )
            if not preserve_verified:
                candidate.title = title
                candidate.normalized_title = normalized_title
                candidate.summary = draft.summary[:4_000] if draft.summary else None
                candidate.verification_status = draft.verification_status
            candidate.query = draft.query.strip()[:1_000]
            candidate.relevance_score = max(0.0, min(1.0, draft.relevance_score))
            candidate.expires_at = expires_at
            await self.session.flush()
            if first_seen:
                result.append(candidate)
        return result

    async def get_candidate(self, user_id: int, candidate_id: int) -> EventCandidate:
        candidate = await self.session.scalar(
            select(EventCandidate).where(
                EventCandidate.id == candidate_id,
                EventCandidate.user_id == user_id,
            )
        )
        if candidate is None:
            raise ValueError("candidate not found")
        return candidate

    async def save_candidate_snapshot(
        self,
        user_id: int,
        candidate_id: int,
        research: VerifiedConferenceResearch,
        relevance: RelevanceAssessment,
        researched_at: datetime,
    ) -> EventCandidate:
        candidate = await self.get_candidate(user_id, candidate_id)
        facts = {item.kind.casefold(): item.value for item in research.facts}
        candidate.start_date = self._iso_date(facts.get("start_date"))
        candidate.end_date = self._iso_date(facts.get("end_date"))
        candidate.location = self._bounded(facts.get("location"), 500)
        candidate.official_url = self._bounded(facts.get("official_url"), 2_048)
        candidate.research_payload = research.model_dump(mode="json")
        candidate.researched_at = researched_at
        candidate.topic_relevance_score = relevance.score
        candidate.topic_relevance_reason = self._bounded(relevance.explanation, 1_000)
        await self.session.flush()
        return candidate

    async def save_candidate_preview(
        self,
        user_id: int,
        candidate_id: int,
        research: ConferenceResearch,
    ) -> EventCandidate:
        candidate = await self.get_candidate(user_id, candidate_id)
        verified_shape = VerifiedConferenceResearch.model_validate(
            research.model_dump(mode="python")
        )
        facts = {item.kind.casefold(): item.value for item in research.facts}
        candidate.start_date = self._iso_date(facts.get("start_date"))
        candidate.end_date = self._iso_date(facts.get("end_date"))
        candidate.location = self._bounded(facts.get("location"), 500)
        candidate.official_url = self._bounded(facts.get("official_url"), 2_048)
        candidate.research_payload = verified_shape.model_dump(mode="json")
        candidate.researched_at = None
        candidate.topic_relevance_score = None
        candidate.topic_relevance_reason = None
        await self.session.flush()
        return candidate

    async def load_candidate_snapshot(
        self, user_id: int, candidate_id: int
    ) -> VerifiedConferenceResearch | None:
        candidate = await self.get_candidate(user_id, candidate_id)
        if not candidate.research_payload:
            return None
        try:
            return VerifiedConferenceResearch.model_validate(candidate.research_payload)
        except (ValidationError, TypeError, ValueError):
            return None

    @staticmethod
    def _iso_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _bounded(value: str | None, limit: int) -> str | None:
        cleaned = value.strip() if value else ""
        return cleaned[:limit] or None

    async def set_candidate_status(
        self,
        user_id: int,
        candidate_id: int,
        status: CandidateStatus,
    ) -> EventCandidate:
        candidate = await self.get_candidate(user_id, candidate_id)
        candidate.status = status
        await self.session.flush()
        return candidate

    async def transition_candidate(
        self,
        user_id: int,
        candidate_id: int,
        status: CandidateStatus,
        reason: CandidateDecisionReason | None,
        source: CandidateDecisionSource,
        decided_at: datetime,
        *,
        next_review_at: datetime | None = None,
    ) -> EventCandidate:
        allowed_reasons = {
            CandidateStatus.ACTIVE: {None},
            CandidateStatus.IMPORTED: {None},
            CandidateStatus.SKIPPED: {
                CandidateDecisionReason.USER_DEFERRED,
                CandidateDecisionReason.LOW_RELEVANCE,
                CandidateDecisionReason.CATALOG_EXPANDED,
                CandidateDecisionReason.LEGACY_SKIPPED,
            },
            CandidateStatus.REJECTED: {CandidateDecisionReason.USER_REJECTED},
            CandidateStatus.EXPIRED: {
                CandidateDecisionReason.EVENT_ENDED,
                CandidateDecisionReason.CANDIDATE_TTL_EXPIRED,
            },
        }
        if reason not in allowed_reasons[status]:
            raise ValueError(f"invalid decision reason for {status.value}")

        candidate = await self.get_candidate(user_id, candidate_id)
        candidate.status = status
        candidate.next_review_at = next_review_at if status == CandidateStatus.SKIPPED else None
        if status in {CandidateStatus.ACTIVE, CandidateStatus.IMPORTED}:
            candidate.decision_reason = None
            candidate.decision_source = None
            candidate.decision_at = None
        else:
            candidate.decision_reason = reason
            candidate.decision_source = source
            candidate.decision_at = decided_at
        await self.session.flush()
        return candidate

    async def list_candidates_by_statuses(
        self,
        user_id: int,
        statuses: list[CandidateStatus],
        *,
        limit: int = 100,
    ) -> list[EventCandidate]:
        if not statuses:
            return []
        return list(
            await self.session.scalars(
                select(EventCandidate)
                .where(
                    EventCandidate.user_id == user_id,
                    EventCandidate.status.in_(statuses),
                )
                .order_by(EventCandidate.relevance_score.desc(), EventCandidate.id)
                .limit(max(1, min(limit, self.MAX_CANDIDATES_PER_LIST)))
            )
        )

    async def list_candidates(
        self,
        user_id: int,
        status: CandidateStatus = CandidateStatus.ACTIVE,
        *,
        limit: int = 20,
    ) -> list[EventCandidate]:
        return list(
            await self.session.scalars(
                select(EventCandidate)
                .where(EventCandidate.user_id == user_id, EventCandidate.status == status)
                .order_by(EventCandidate.relevance_score.desc(), EventCandidate.id)
                .limit(max(1, min(limit, self.MAX_CANDIDATES_PER_LIST)))
            )
        )

    async def add_alias(self, conference_id: int, alias: str) -> ConferenceAlias:
        normalized = normalize_name(alias)
        existing = await self.session.scalar(
            select(ConferenceAlias).where(
                ConferenceAlias.conference_id == conference_id,
                ConferenceAlias.normalized_alias == normalized,
            )
        )
        if existing is not None:
            return existing
        record = ConferenceAlias(
            conference_id=conference_id,
            alias=alias.strip()[:500],
            normalized_alias=normalized,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def add_evidence(
        self,
        conference_id: int,
        *,
        fact_kind: str,
        fact_value: str,
        source_url: str,
        source_class: EvidenceSourceClass,
        excerpt: str,
        verification_status: VerificationStatus,
        observed_at: datetime,
    ) -> ConferenceEvidence:
        bounded_kind = fact_kind.strip()[:100]
        bounded_value = fact_value[: self.MAX_FACT_VALUE]
        existing = await self.session.scalar(
            select(ConferenceEvidence).where(
                ConferenceEvidence.conference_id == conference_id,
                ConferenceEvidence.fact_kind == bounded_kind,
                ConferenceEvidence.fact_value == bounded_value,
                ConferenceEvidence.source_url == source_url,
            )
        )
        if existing is not None:
            existing.source_class = source_class
            existing.excerpt = excerpt[: self.MAX_EVIDENCE_EXCERPT]
            existing.verification_status = verification_status
            existing.observed_at = observed_at
            existing.active = True
            await self.session.flush()
            return existing
        record = ConferenceEvidence(
            conference_id=conference_id,
            fact_kind=bounded_kind,
            fact_value=bounded_value,
            source_url=source_url,
            source_class=source_class,
            excerpt=excerpt[: self.MAX_EVIDENCE_EXCERPT],
            verification_status=verification_status,
            observed_at=observed_at,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def list_evidence(self, conference_id: int) -> list[ConferenceEvidence]:
        return list(
            await self.session.scalars(
                select(ConferenceEvidence)
                .where(ConferenceEvidence.conference_id == conference_id)
                .order_by(ConferenceEvidence.observed_at, ConferenceEvidence.id)
            )
        )

    async def record_attempt(
        self,
        user_id: int,
        conference_id: int,
        status: MonitoringAttemptStatus,
        *,
        source_url: str | None = None,
        error_code: str | None = None,
        observed_at: datetime | None = None,
    ) -> MonitoringAttempt:
        attempt = MonitoringAttempt(
            user_id=user_id,
            conference_id=conference_id,
            status=status,
            source_url=source_url,
            error_code=error_code[:100] if error_code else None,
            observed_at=observed_at or datetime.now(UTC),
        )
        self.session.add(attempt)
        await self.session.flush()
        return attempt

    async def record_change(
        self,
        conference_id: int,
        *,
        field_name: str,
        old_value: str | None,
        new_value: str | None,
        source_url: str,
        verification_status: VerificationStatus,
        detected_at: datetime | None = None,
    ) -> ConferenceChange:
        change = ConferenceChange(
            conference_id=conference_id,
            field_name=field_name[:100],
            old_value=old_value,
            new_value=new_value,
            source_url=source_url,
            verification_status=verification_status,
            detected_at=detected_at or datetime.now(UTC),
        )
        self.session.add(change)
        await self.session.flush()
        return change

    async def list_changes(self, conference_id: int) -> list[ConferenceChange]:
        return list(
            await self.session.scalars(
                select(ConferenceChange)
                .where(ConferenceChange.conference_id == conference_id)
                .order_by(ConferenceChange.detected_at, ConferenceChange.id)
            )
        )
