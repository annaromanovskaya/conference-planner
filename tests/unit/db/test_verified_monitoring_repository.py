from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import Conference, Deadline
from conference_scout.db.repositories.conference_research import (
    CandidateDraft,
    ConferenceResearchRepository,
)
from conference_scout.db.repositories.users import UserRepository
from conference_scout.schemas import common as common_schemas
from conference_scout.schemas.common import (
    CandidateStatus,
    DeadlineType,
    EvidenceSourceClass,
    MonitoringAttemptStatus,
    VerificationStatus,
)


@pytest.mark.asyncio
async def test_candidates_are_durable_deduplicated_and_owner_scoped(
    session: AsyncSession,
) -> None:
    users = UserRepository(session)
    first = await users.get_or_create_by_telegram_id(101)
    second = await users.get_or_create_by_telegram_id(202)
    repository = ConferenceResearchRepository(session)
    expires_at = datetime.now(UTC) + timedelta(days=2)
    draft = CandidateDraft(
        title="IMBASE 2027",
        source_url="https://meetings.iac.es/imbase2027/",
        query="binary stars 2027",
        relevance_score=0.95,
        summary="Official meeting page",
        verification_status=VerificationStatus.OFFICIAL_CONFIRMED,
    )

    created = await repository.create_candidates(first.id, [draft, draft], expires_at=expires_at)
    await repository.create_candidates(second.id, [draft], expires_at=expires_at)
    await session.commit()

    assert len(created) == 1
    assert (await repository.get_candidate(first.id, created[0].id)).title == "IMBASE 2027"
    assert len(await repository.list_candidates(first.id)) == 1
    assert len(await repository.list_candidates(second.id)) == 1
    with pytest.raises(ValueError, match="candidate not found"):
        await repository.get_candidate(second.id, created[0].id)


@pytest.mark.asyncio
async def test_candidates_with_same_url_are_distinguished_by_normalized_title(
    session: AsyncSession,
) -> None:
    users = UserRepository(session)
    first = await users.get_or_create_by_telegram_id(101)
    second = await users.get_or_create_by_telegram_id(202)
    repository = ConferenceResearchRepository(session)
    expires_at = datetime.now(UTC) + timedelta(days=2)
    shared_url = "https://www.sai.msu.ru/conference/annconf/index.html"

    created = await repository.create_candidates(
        first.id,
        [
            CandidateDraft(
                title="Stellar Astronomy 2026",
                source_url=shared_url,
                query="stellar astronomy",
                relevance_score=0.9,
                summary="First version",
                verification_status=VerificationStatus.UNVERIFIABLE,
            ),
            CandidateDraft(
                title="Binary Stars 2027",
                source_url=shared_url,
                query="stellar astronomy",
                relevance_score=0.8,
                verification_status=VerificationStatus.TENTATIVE,
            ),
            CandidateDraft(
                title="  STELLAR   ASTRONOMY 2026 ",
                source_url=shared_url,
                query="updated query",
                relevance_score=1.0,
                summary="Updated",
                verification_status=VerificationStatus.OFFICIAL_CONFIRMED,
            ),
        ],
        expires_at=expires_at,
    )
    other_user = await repository.create_candidates(
        second.id,
        [
            CandidateDraft(
                title="Stellar Astronomy 2026",
                source_url=shared_url,
                query="stellar astronomy",
                relevance_score=0.7,
                verification_status=VerificationStatus.UNVERIFIABLE,
            )
        ],
        expires_at=expires_at,
    )
    await session.commit()

    first_user_candidates = await repository.list_candidates(first.id)
    assert len(created) == 2
    assert len(first_user_candidates) == 2
    assert len(other_user) == 1
    updated = next(
        item for item in first_user_candidates if item.normalized_title == "stellar astronomy 2026"
    )
    assert updated.summary == "Updated"
    assert updated.query == "updated query"
    assert updated.verification_status == VerificationStatus.OFFICIAL_CONFIRMED


@pytest.mark.asyncio
async def test_candidate_status_transitions_are_owner_scoped(session: AsyncSession) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(101)
    other = await UserRepository(session).get_or_create_by_telegram_id(202)
    repository = ConferenceResearchRepository(session)
    candidate = (
        await repository.create_candidates(
            owner.id,
            [
                CandidateDraft(
                    title="IMBASE 2027",
                    source_url="https://meetings.iac.es/imbase2027/",
                    query="IMBASE",
                    relevance_score=1.0,
                    verification_status=VerificationStatus.OFFICIAL_CONFIRMED,
                )
            ],
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )[0]

    decided_at = datetime(2026, 10, 20, 12, tzinfo=UTC)
    next_review_at = decided_at + timedelta(days=7)
    skipped = await repository.transition_candidate(
        owner.id,
        candidate.id,
        CandidateStatus.SKIPPED,
        common_schemas.CandidateDecisionReason.USER_DEFERRED,
        common_schemas.CandidateDecisionSource.USER,
        decided_at,
        next_review_at=next_review_at,
    )

    assert skipped.status == CandidateStatus.SKIPPED
    assert skipped.decision_reason == common_schemas.CandidateDecisionReason.USER_DEFERRED
    assert skipped.decision_source == common_schemas.CandidateDecisionSource.USER
    assert skipped.decision_at == decided_at
    assert skipped.next_review_at == next_review_at
    assert [item.id for item in await repository.list_candidates_by_statuses(
        owner.id, [CandidateStatus.SKIPPED, CandidateStatus.REJECTED]
    )] == [candidate.id]

    restored = await repository.transition_candidate(
        owner.id,
        candidate.id,
        CandidateStatus.ACTIVE,
        None,
        common_schemas.CandidateDecisionSource.USER,
        decided_at + timedelta(minutes=1),
    )
    assert restored.status == CandidateStatus.ACTIVE
    assert restored.decision_reason is None
    assert restored.next_review_at is None

    rejected = await repository.transition_candidate(
        owner.id,
        candidate.id,
        CandidateStatus.REJECTED,
        common_schemas.CandidateDecisionReason.USER_REJECTED,
        common_schemas.CandidateDecisionSource.USER,
        decided_at + timedelta(minutes=2),
        next_review_at=decided_at + timedelta(days=30),
    )
    assert rejected.status == CandidateStatus.REJECTED
    assert rejected.decision_reason == common_schemas.CandidateDecisionReason.USER_REJECTED
    assert rejected.next_review_at is None
    assert await repository.list_candidates(owner.id, CandidateStatus.ACTIVE) == []
    with pytest.raises(ValueError, match="candidate not found"):
        await repository.transition_candidate(
            other.id,
            candidate.id,
            CandidateStatus.IMPORTED,
            None,
            common_schemas.CandidateDecisionSource.USER,
            decided_at,
        )


@pytest.mark.asyncio
async def test_alias_evidence_attempt_and_change_records_are_bounded(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(101)
    conference = Conference(name="IMBASE 2027", normalized_name="imbase 2027")
    session.add(conference)
    await session.flush()
    repository = ConferenceResearchRepository(session)

    alias = await repository.add_alias(conference.id, " ImBaSE 2027 ")
    duplicate = await repository.add_alias(conference.id, "imbase 2027")
    evidence = await repository.add_evidence(
        conference.id,
        fact_kind="event_dates",
        fact_value="2027-07-12/2027-07-16",
        source_url="https://meetings.iac.es/imbase2027/pages/important-dates.php",
        source_class=EvidenceSourceClass.OFFICIAL_CONFERENCE,
        excerpt="x" * 5_000,
        verification_status=VerificationStatus.OFFICIAL_CONFIRMED,
        observed_at=datetime.now(UTC),
    )
    attempt = await repository.record_attempt(
        owner.id,
        conference.id,
        MonitoringAttemptStatus.SUCCEEDED,
        source_url="https://meetings.iac.es/imbase2027/",
    )
    change = await repository.record_change(
        conference.id,
        field_name="start_date",
        old_value=None,
        new_value="2027-07-12",
        source_url="https://meetings.iac.es/imbase2027/",
        verification_status=VerificationStatus.OFFICIAL_CONFIRMED,
    )
    await session.commit()

    assert alias.id == duplicate.id
    assert alias.normalized_alias == "imbase 2027"
    assert len(evidence.excerpt) == repository.MAX_EVIDENCE_EXCERPT
    assert [item.id for item in await repository.list_evidence(conference.id)] == [evidence.id]
    assert [item.id for item in await repository.list_changes(conference.id)] == [change.id]
    assert attempt.error_code is None


@pytest.mark.asyncio
async def test_deadline_accepts_exact_or_approximate_period_but_not_both(
    session: AsyncSession,
) -> None:
    conference = Conference(name="IMBASE 2027", normalized_name="imbase 2027")
    session.add(conference)
    await session.flush()
    session.add_all(
        [
            Deadline(
                conference_id=conference.id,
                deadline_type=DeadlineType.REGISTRATION_OPEN,
                approximate_year=2026,
                approximate_month=11,
                tentative=True,
                source_url="https://meetings.iac.es/imbase2027/pages/important-dates.php",
            ),
            Deadline(
                conference_id=conference.id,
                deadline_type=DeadlineType.OTHER,
                deadline_date=date(2027, 7, 12),
                source_url="https://meetings.iac.es/imbase2027/",
            ),
        ]
    )
    await session.commit()

    stored = list(await session.scalars(select(Deadline).order_by(Deadline.id)))
    assert stored[0].deadline_date is None
    assert (stored[0].approximate_year, stored[0].approximate_month) == (2026, 11)
    assert stored[1].deadline_date == date(2027, 7, 12)

    session.add(
        Deadline(
            conference_id=conference.id,
            deadline_type=DeadlineType.PAYMENT,
            approximate_year=2027,
            approximate_month=13,
            source_url="https://meetings.iac.es/imbase2027/",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
