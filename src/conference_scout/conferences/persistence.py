from dataclasses import dataclass, replace
from datetime import date
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.conferences.deduplication import normalize_name
from conference_scout.db.base import utc_now
from conference_scout.db.models import (
    Conference,
    Deadline,
    DeadlineHistory,
)
from conference_scout.db.repositories.conference_research import (
    ConferenceResearchRepository,
)
from conference_scout.db.repositories.conferences import ConferenceRepository
from conference_scout.schemas.common import (
    DeadlineType,
    EvidenceSourceClass,
    VerificationStatus,
)
from conference_scout.schemas.research import (
    DeadlineClaim,
    FactClaim,
    VerifiedConferenceResearch,
)


@dataclass(frozen=True, slots=True)
class PersistedDeadlineView:
    id: int
    deadline_type: DeadlineType
    exact_date: date | None
    approximate_year: int | None
    approximate_month: int | None
    tentative: bool
    verification_status: VerificationStatus
    source_url: str


@dataclass(frozen=True, slots=True)
class PersistenceReceipt:
    conference_id: int
    conference_name: str
    committed: bool
    created: bool
    tracked: bool
    subscription_id: int | None
    created_fields: tuple[str, ...]
    updated_fields: tuple[str, ...]
    deadlines: tuple[PersistedDeadlineView, ...]
    evidence_ids: tuple[int, ...]
    change_ids: tuple[int, ...]

    def telegram_summary(self) -> str:
        if not self.committed:
            raise RuntimeError("cannot summarize an uncommitted persistence receipt")
        if self.created:
            parts = [f"Конференция «{self.conference_name}» сохранена."]
            if self.deadlines:
                parts.append(f"Дедлайнов сохранено: {len(self.deadlines)}.")
            if self.tracked:
                parts.append("Отслеживание включено.")
            return " ".join(parts)
        changed_fields = tuple(dict.fromkeys((*self.created_fields, *self.updated_fields)))
        if changed_fields:
            return "Обновлено: " + ", ".join(changed_fields) + "."
        return "Изменений нет. Данные конференции уже актуальны."


class ConferencePersistenceService:
    _SOURCE_RANK: ClassVar[dict[EvidenceSourceClass, int]] = {
        EvidenceSourceClass.SECONDARY: 1,
        EvidenceSourceClass.OFFICIAL_ORGANIZER: 2,
        EvidenceSourceClass.OFFICIAL_CONFERENCE: 3,
    }
    _USABLE_STATUSES: ClassVar[set[VerificationStatus]] = {
        VerificationStatus.OFFICIAL_CONFIRMED,
        VerificationStatus.ORGANIZER_CONFIRMED,
        VerificationStatus.SECONDARY_UNVERIFIED,
        VerificationStatus.TENTATIVE,
    }
    _FACT_FIELDS: ClassVar[dict[str, str]] = {
        "official_url": "website",
        "start_date": "start_date",
        "end_date": "end_date",
        "location": "location",
        "description": "description",
        "acronym": "acronym",
        "event_type": "event_type",
    }

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.conferences = ConferenceRepository(session)
        self.research = ConferenceResearchRepository(session)

    async def persist(
        self,
        user_id: int,
        verified: VerifiedConferenceResearch,
        *,
        track: bool,
    ) -> PersistenceReceipt:
        try:
            receipt = await self._persist_and_reread(user_id, verified, track=track)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return replace(receipt, committed=True)

    async def persist_deadline(
        self,
        user_id: int,
        conference_id: int,
        claim: DeadlineClaim,
    ) -> PersistenceReceipt:
        """Persist one deadline against the exact owner-accessible conference ID."""
        try:
            conference = await self.conferences.require_accessible(
                user_id, conference_id
            )
            evidence_id, changed, change_id = await self._persist_deadline(
                conference, claim
            )
            field_name = f"deadline:{claim.deadline_type.value}"
            await self.session.flush()
            receipt = await self._reread(
                user_id,
                conference.id,
                created=False,
                created_fields=(field_name,) if changed == "created" else (),
                updated_fields=(field_name,) if changed == "updated" else (),
                evidence_ids=(evidence_id,),
                change_ids=(change_id,) if change_id is not None else (),
            )
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return replace(receipt, committed=True)

    async def persist_for_conference(
        self,
        user_id: int,
        conference_id: int,
        verified: VerifiedConferenceResearch,
    ) -> PersistenceReceipt:
        """Persist monitoring results into the exact owner-accessible record."""
        try:
            conference = await self.conferences.require_accessible(
                user_id, conference_id
            )
            receipt = await self._persist_claims_and_reread(
                user_id,
                conference,
                verified,
                created=False,
                track=False,
            )
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return replace(receipt, committed=True)

    async def _persist_and_reread(
        self,
        user_id: int,
        verified: VerifiedConferenceResearch,
        *,
        track: bool,
    ) -> PersistenceReceipt:
        official_urls = {item.value for item in verified.facts if item.kind == "official_url"}
        urls = {str(verified.source_url), *official_urls}
        conference = await self.conferences.find_match(verified.name, urls)
        created = conference is None
        created_fields: list[str] = []
        if conference is None:
            conference = Conference(
                name=verified.name,
                normalized_name=normalize_name(verified.name),
                source_url=str(verified.source_url),
                last_checked_at=utc_now(),
            )
            self.session.add(conference)
            await self.session.flush()
            created_fields.extend(("name", "source_url"))

        return await self._persist_claims_and_reread(
            user_id,
            conference,
            verified,
            created=created,
            track=track,
            created_fields=created_fields,
        )

    async def _persist_claims_and_reread(
        self,
        user_id: int,
        conference: Conference,
        verified: VerifiedConferenceResearch,
        *,
        created: bool,
        track: bool,
        created_fields: list[str] | None = None,
    ) -> PersistenceReceipt:
        created_fields = list(created_fields or ())
        updated_fields: list[str] = []
        evidence_ids: list[int] = []
        change_ids: list[int] = []

        for alias in (verified.name, *verified.aliases):
            await self.research.add_alias(conference.id, alias)

        for claim in verified.facts:
            evidence_id, changed, change_id = await self._persist_fact(conference, claim)
            evidence_ids.append(evidence_id)
            if changed:
                target = updated_fields if not created and changed == "updated" else created_fields
                target.append(claim.kind)
            if change_id is not None:
                change_ids.append(change_id)

        for claim in verified.deadlines:
            evidence_id, changed, change_id = await self._persist_deadline(conference, claim)
            evidence_ids.append(evidence_id)
            field_name = f"deadline:{claim.deadline_type.value}"
            if changed:
                target = updated_fields if not created and changed == "updated" else created_fields
                target.append(field_name)
            if change_id is not None:
                change_ids.append(change_id)

        conference.last_checked_at = utc_now()
        if track:
            await self.conferences.track(user_id, conference.id)
        await self.session.flush()
        return await self._reread(
            user_id,
            conference.id,
            created=created,
            created_fields=tuple(dict.fromkeys(created_fields)),
            updated_fields=tuple(dict.fromkeys(updated_fields)),
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            change_ids=tuple(dict.fromkeys(change_ids)),
        )

    async def _persist_fact(
        self, conference: Conference, claim: FactClaim
    ) -> tuple[int, str | None, int | None]:
        previous = [
            item
            for item in await self.research.list_evidence(conference.id)
            if item.fact_kind == claim.kind and item.active
        ]
        evidence = await self.research.add_evidence(
            conference.id,
            fact_kind=claim.kind,
            fact_value=claim.value,
            source_url=str(claim.source_url),
            source_class=claim.source_class,
            excerpt=claim.excerpt,
            verification_status=claim.verification_status,
            observed_at=utc_now(),
        )
        field_name = self._FACT_FIELDS.get(claim.kind)
        if field_name is None or claim.verification_status not in self._USABLE_STATUSES:
            evidence.active = False
            return evidence.id, None, None
        try:
            new_value = self._coerce_fact_value(field_name, claim.value)
        except ValueError:
            evidence.active = False
            evidence.verification_status = VerificationStatus.UNVERIFIABLE
            return evidence.id, None, None
        old_value = getattr(conference, field_name)
        if old_value == new_value:
            return evidence.id, None, None

        old_rank = max((self._SOURCE_RANK[item.source_class] for item in previous), default=0)
        new_rank = self._SOURCE_RANK[claim.source_class]
        same_source = bool(previous) and all(
            item.source_url == str(claim.source_url) for item in previous
        )
        if old_value is not None and (
            new_rank < old_rank or (new_rank == old_rank and not same_source)
        ):
            evidence.active = False
            if new_rank == old_rank:
                evidence.verification_status = VerificationStatus.CONFLICT
            return evidence.id, None, None

        setattr(conference, field_name, new_value)
        for item in previous:
            if item.fact_value != claim.value:
                item.active = False
        change_id = None
        changed = "created" if old_value is None else "updated"
        if old_value is not None:
            change = await self.research.record_change(
                conference.id,
                field_name=claim.kind,
                old_value=self._serialize_value(old_value),
                new_value=self._serialize_value(new_value),
                source_url=str(claim.source_url),
                verification_status=claim.verification_status,
            )
            change_id = change.id
        return evidence.id, changed, change_id

    async def _persist_deadline(
        self, conference: Conference, claim: DeadlineClaim
    ) -> tuple[int, str | None, int | None]:
        fact_kind = f"deadline:{claim.deadline_type.value}"
        fact_value = self._deadline_value(claim)
        evidence = await self.research.add_evidence(
            conference.id,
            fact_kind=fact_kind,
            fact_value=fact_value,
            source_url=str(claim.source_url),
            source_class=claim.source_class,
            excerpt=claim.excerpt,
            verification_status=claim.verification_status,
            observed_at=utc_now(),
        )
        if claim.verification_status not in self._USABLE_STATUSES:
            evidence.active = False
            return evidence.id, None, None

        deadline = await self.session.scalar(
            select(Deadline).where(
                Deadline.conference_id == conference.id,
                Deadline.deadline_type == claim.deadline_type,
            )
        )
        if deadline is None:
            deadline = Deadline(conference_id=conference.id, deadline_type=claim.deadline_type)
            self.session.add(deadline)
            changed = "created"
        else:
            old_value = self._stored_deadline_value(deadline)
            if old_value == fact_value and deadline.tentative == claim.tentative:
                if (
                    self._SOURCE_RANK[claim.source_class]
                    >= self._SOURCE_RANK[deadline.source_class]
                ):
                    self._apply_deadline_metadata(deadline, claim)
                return evidence.id, None, None
            new_rank = self._SOURCE_RANK[claim.source_class]
            old_rank = self._SOURCE_RANK[deadline.source_class]
            same_source = deadline.source_url == str(claim.source_url)
            if new_rank < old_rank or (new_rank == old_rank and not same_source):
                evidence.active = False
                if (
                    self._SOURCE_RANK[claim.source_class]
                    == self._SOURCE_RANK[deadline.source_class]
                ):
                    evidence.verification_status = VerificationStatus.CONFLICT
                return evidence.id, None, None
            changed = "updated"

        old_date = deadline.deadline_date
        old_serialized = self._stored_deadline_value(deadline) if deadline.id is not None else None
        self._apply_deadline_metadata(deadline, claim)
        await self.session.flush()
        change_id = None
        if changed == "updated":
            change = await self.research.record_change(
                conference.id,
                field_name=fact_kind,
                old_value=old_serialized,
                new_value=fact_value,
                source_url=str(claim.source_url),
                verification_status=claim.verification_status,
            )
            change_id = change.id
            if (
                old_date is not None
                and claim.exact_date is not None
                and old_date != claim.exact_date
            ):
                self.session.add(
                    DeadlineHistory(
                        conference_id=conference.id,
                        deadline_type=claim.deadline_type,
                        old_date=old_date,
                        new_date=claim.exact_date,
                        source_url=str(claim.source_url),
                    )
                )
        return evidence.id, changed, change_id

    @staticmethod
    def _apply_deadline_metadata(deadline: Deadline, claim: DeadlineClaim) -> None:
        deadline.deadline_date = claim.exact_date
        deadline.approximate_year = claim.approximate_year
        deadline.approximate_month = claim.approximate_month
        deadline.tentative = claim.tentative
        deadline.source_url = str(claim.source_url)
        deadline.source_class = claim.source_class
        deadline.verification_status = claim.verification_status
        deadline.verified = claim.verification_status in {
            VerificationStatus.OFFICIAL_CONFIRMED,
            VerificationStatus.ORGANIZER_CONFIRMED,
        }
        deadline.evidence_excerpt = claim.excerpt
        deadline.observed_at = utc_now()
        deadline.last_checked_at = utc_now()

    async def _reread(
        self,
        user_id: int,
        conference_id: int,
        *,
        created: bool,
        created_fields: tuple[str, ...],
        updated_fields: tuple[str, ...],
        evidence_ids: tuple[int, ...],
        change_ids: tuple[int, ...],
    ) -> PersistenceReceipt:
        conference = await self.session.get(Conference, conference_id, populate_existing=True)
        if conference is None:
            raise RuntimeError("conference read-back failed")
        subscription = await self.conferences.get_subscription(user_id, conference_id)
        deadlines = tuple(
            PersistedDeadlineView(
                id=item.id,
                deadline_type=item.deadline_type,
                exact_date=item.deadline_date,
                approximate_year=item.approximate_year,
                approximate_month=item.approximate_month,
                tentative=item.tentative,
                verification_status=item.verification_status,
                source_url=item.source_url,
            )
            for item in await self.conferences.list_deadlines(conference_id)
        )
        return PersistenceReceipt(
            conference_id=conference.id,
            conference_name=conference.name,
            committed=False,
            created=created,
            tracked=subscription is not None,
            subscription_id=subscription.id if subscription else None,
            created_fields=created_fields,
            updated_fields=updated_fields,
            deadlines=deadlines,
            evidence_ids=evidence_ids,
            change_ids=change_ids,
        )

    @staticmethod
    def _coerce_fact_value(field_name: str, value: str) -> str | date:
        if field_name in {"start_date", "end_date"}:
            return date.fromisoformat(value)
        return value

    @staticmethod
    def _serialize_value(value: object) -> str:
        return value.isoformat() if isinstance(value, date) else str(value)

    @staticmethod
    def _deadline_value(claim: DeadlineClaim) -> str:
        if claim.exact_date is not None:
            return claim.exact_date.isoformat()
        month = f"-{claim.approximate_month:02d}" if claim.approximate_month else ""
        return f"{claim.approximate_year}{month}"

    @staticmethod
    def _stored_deadline_value(deadline: Deadline) -> str:
        if deadline.deadline_date is not None:
            return deadline.deadline_date.isoformat()
        month = f"-{deadline.approximate_month:02d}" if deadline.approximate_month else ""
        return f"{deadline.approximate_year}{month}"
