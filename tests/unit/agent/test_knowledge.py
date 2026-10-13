from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.agent.knowledge import KnowledgeGraphService
from conference_scout.db.models import (
    Conference,
    ConferenceTopic,
    Deadline,
    DeadlineHistory,
    Subscription,
    Topic,
)
from conference_scout.db.repositories.memories import KnowledgeRelationRepository, MemoryRepository
from conference_scout.db.repositories.users import UserRepository
from conference_scout.schemas.common import (
    DeadlineType,
    KnowledgeRelationKind,
    MemoryKind,
    MemoryScope,
)


async def _seed_graph(session: AsyncSession) -> tuple[int, int, int]:
    users = UserRepository(session)
    owner = await users.get_or_create_by_telegram_id(123)
    other = await users.get_or_create_by_telegram_id(456)
    topic = Topic(
        user_id=owner.id,
        name="Stellar spectroscopy",
        description="Spectra of stars",
        keywords=["stellar spectra"],
    )
    private_topic = Topic(
        user_id=other.id,
        name="Private biology",
        description="Private",
        keywords=["biology"],
    )
    primary = Conference(
        name="Stellar Spectroscopy 2027",
        normalized_name="stellar spectroscopy 2027",
        description="Spectra and stellar atmospheres",
        website="https://astro.example/event",
        source_url="https://astro.example/event",
    )
    related = Conference(
        name="Astroinformatics 2027",
        normalized_name="astroinformatics 2027",
        description="Machine learning for spectra",
    )
    private = Conference(name="Private Biology", normalized_name="private biology")
    session.add_all([topic, private_topic, primary, related, private])
    await session.flush()
    session.add_all(
        [
            ConferenceTopic(conference_id=primary.id, topic_id=topic.id, relevance_score=0.95),
            ConferenceTopic(conference_id=related.id, topic_id=topic.id, relevance_score=0.75),
            ConferenceTopic(
                conference_id=private.id, topic_id=private_topic.id, relevance_score=0.99
            ),
            Subscription(user_id=owner.id, conference_id=primary.id),
        ]
    )
    deadline = Deadline(
        conference_id=primary.id,
        deadline_type=DeadlineType.ABSTRACT_SUBMISSION,
        deadline_date=date(2027, 2, 12),
        source_url="https://astro.example/deadlines",
        verified=True,
        last_checked_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    session.add(deadline)
    await session.flush()
    session.add(
        DeadlineHistory(
            conference_id=primary.id,
            deadline_type=DeadlineType.ABSTRACT_SUBMISSION,
            old_date=date(2027, 2, 5),
            new_date=date(2027, 2, 12),
            detected_at=datetime(2026, 10, 13, tzinfo=UTC),
            source_url="https://astro.example/deadlines",
        )
    )
    await MemoryRepository(session).create(
        owner.id,
        kind=MemoryKind.NOTE,
        scope=MemoryScope.CONFERENCE,
        original_text="High priority",
        structured_data={"priority": "high"},
        provenance={"telegram_message_id": 9},
        conference_id=primary.id,
    )
    await KnowledgeRelationRepository(session).create(
        owner.id,
        source_type="conference",
        source_id=primary.id,
        relation_type=KnowledgeRelationKind.RELATED_TO,
        target_type="conference",
        target_id=related.id,
        provenance={"reason": "shared topic"},
    )
    await session.commit()
    return owner.id, primary.id, related.id


@pytest.mark.asyncio
async def test_conference_context_projects_verified_relations_and_provenance(
    session: AsyncSession,
) -> None:
    owner_id, conference_id, _ = await _seed_graph(session)

    graph = await KnowledgeGraphService(session).conference_context(owner_id, conference_id)

    predicates = {edge.predicate for edge in graph.edges}
    assert {
        "RELEVANT_TO",
        "HAS_DEADLINE",
        "VERIFIED_BY",
        "TRACKS",
        "CHANGED_FROM",
        "APPLIES_TO",
        "RELATED_TO",
    } <= predicates
    deadline = next(node for node in graph.nodes if node.kind == "deadline")
    assert deadline.provenance == {
        "source_url": "https://astro.example/deadlines",
        "verified": True,
    }
    assert deadline.observed_at is not None


@pytest.mark.asyncio
async def test_search_is_normalized_bounded_and_excludes_other_owner(
    session: AsyncSession,
) -> None:
    owner_id, _, _ = await _seed_graph(session)

    graph = await KnowledgeGraphService(session).search(owner_id, "SPECTROSCOPY", limit=1)

    conference_nodes = [node for node in graph.nodes if node.kind == "conference"]
    assert [node.label for node in conference_nodes] == ["Stellar Spectroscopy 2027"]
    assert all(node.label != "Private Biology" for node in graph.nodes)


@pytest.mark.asyncio
async def test_related_conferences_are_owner_scoped_one_hop(session: AsyncSession) -> None:
    owner_id, conference_id, related_id = await _seed_graph(session)
    other = await UserRepository(session).get_or_create_by_telegram_id(999)

    graph = await KnowledgeGraphService(session).related_conferences(
        owner_id, conference_id, limit=5
    )
    private_graph = await KnowledgeGraphService(session).related_conferences(
        other.id, conference_id, limit=5
    )

    assert any(node.id == related_id and node.kind == "conference" for node in graph.nodes)
    assert [edge.predicate for edge in graph.edges] == ["RELATED_TO"]
    assert private_graph.nodes == ()
    assert private_graph.edges == ()
