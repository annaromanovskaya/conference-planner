from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import (
    Conference,
    ConferenceTopic,
    Deadline,
    DeadlineHistory,
    KnowledgeRelation,
    Subscription,
    Topic,
    UserMemory,
)
from conference_scout.schemas.agent import KnowledgeEdge, KnowledgeNode, KnowledgeSubgraph


class KnowledgeGraphService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(self, user_id: int, query: str, limit: int) -> KnowledgeSubgraph:
        accessible = await self._accessible_conference_ids(user_id)
        if not accessible or limit < 1:
            return KnowledgeSubgraph()
        conferences = list(
            await self.session.scalars(
                select(Conference).where(Conference.id.in_(accessible)).order_by(Conference.id)
            )
        )
        tokens = [token for token in query.casefold().split() if token]
        matches = [
            conference
            for conference in conferences
            if all(token in self._search_text(conference) for token in tokens)
        ][:limit]
        return KnowledgeSubgraph(
            nodes=tuple(self._conference_node(conference) for conference in matches)
        )

    async def conference_context(self, user_id: int, conference_id: int) -> KnowledgeSubgraph:
        accessible = await self._accessible_conference_ids(user_id)
        if conference_id not in accessible:
            return KnowledgeSubgraph()
        conference = await self.session.get(Conference, conference_id)
        if conference is None:
            return KnowledgeSubgraph()

        nodes: dict[str, KnowledgeNode] = {}
        edges: list[KnowledgeEdge] = []
        conference_key = f"conference:{conference.id}"
        nodes[conference_key] = KnowledgeNode(
            kind="conference",
            id=conference.id,
            label=conference.name,
            attributes={
                "website": conference.website,
                "start_date": conference.start_date,
                "end_date": conference.end_date,
                "location": conference.location,
            },
            provenance={"source_url": conference.source_url},
            observed_at=conference.updated_at,
        )

        topic_rows = await self.session.execute(
            select(ConferenceTopic, Topic)
            .join(Topic, Topic.id == ConferenceTopic.topic_id)
            .where(
                ConferenceTopic.conference_id == conference_id,
                Topic.user_id == user_id,
            )
            .order_by(Topic.id)
        )
        for link, topic in topic_rows:
            key = f"topic:{topic.id}"
            nodes[key] = KnowledgeNode(
                kind="topic",
                id=topic.id,
                label=topic.name,
                attributes={"description": topic.description, "keywords": topic.keywords},
                observed_at=topic.updated_at,
            )
            edges.append(
                KnowledgeEdge(
                    source=conference_key,
                    predicate="RELEVANT_TO",
                    target=key,
                    attributes={"score": link.relevance_score},
                    observed_at=topic.updated_at,
                )
            )

        deadlines = list(
            await self.session.scalars(
                select(Deadline)
                .where(Deadline.conference_id == conference_id)
                .order_by(Deadline.id)
            )
        )
        for deadline in deadlines:
            key = f"deadline:{deadline.id}"
            source_key = f"source:{deadline.source_url}"
            nodes[key] = KnowledgeNode(
                kind="deadline",
                id=deadline.id,
                label=f"{deadline.deadline_type.value}: {deadline.deadline_date.isoformat()}",
                attributes={
                    "type": deadline.deadline_type.value,
                    "date": deadline.deadline_date,
                },
                provenance={
                    "source_url": deadline.source_url,
                    "verified": deadline.verified,
                },
                observed_at=deadline.last_checked_at,
            )
            nodes[source_key] = KnowledgeNode(
                kind="source",
                id=deadline.source_url,
                label=deadline.source_url,
                provenance={"verified": deadline.verified},
                observed_at=deadline.last_checked_at,
            )
            edges.extend(
                [
                    KnowledgeEdge(
                        source=conference_key,
                        predicate="HAS_DEADLINE",
                        target=key,
                        observed_at=deadline.last_checked_at,
                    ),
                    KnowledgeEdge(
                        source=key,
                        predicate="VERIFIED_BY",
                        target=source_key,
                        provenance={"source_url": deadline.source_url},
                        observed_at=deadline.last_checked_at,
                    ),
                ]
            )

        histories = list(
            await self.session.scalars(
                select(DeadlineHistory)
                .where(DeadlineHistory.conference_id == conference_id)
                .order_by(DeadlineHistory.id)
            )
        )
        for history in histories:
            current = next(
                (item for item in deadlines if item.deadline_type == history.deadline_type),
                None,
            )
            if current is None:
                continue
            date_key = f"date:{history.old_date.isoformat()}"
            nodes[date_key] = KnowledgeNode(
                kind="date",
                id=history.old_date.isoformat(),
                label=history.old_date.isoformat(),
                provenance={"source_url": history.source_url},
                observed_at=history.detected_at,
            )
            edges.append(
                KnowledgeEdge(
                    source=f"deadline:{current.id}",
                    predicate="CHANGED_FROM",
                    target=date_key,
                    attributes={"new_date": history.new_date},
                    provenance={"source_url": history.source_url},
                    observed_at=history.detected_at,
                )
            )

        tracked = await self.session.scalar(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.conference_id == conference_id,
            )
        )
        if tracked is not None:
            owner_key = f"owner:{user_id}"
            nodes[owner_key] = KnowledgeNode(kind="owner", id=user_id, label="Owner")
            edges.append(
                KnowledgeEdge(
                    source=owner_key,
                    predicate="TRACKS",
                    target=conference_key,
                    observed_at=tracked.created_at,
                )
            )

        memories = list(
            await self.session.scalars(
                select(UserMemory).where(
                    UserMemory.user_id == user_id,
                    UserMemory.conference_id == conference_id,
                    UserMemory.active.is_(True),
                )
            )
        )
        for memory in memories:
            key = f"memory:{memory.id}"
            nodes[key] = KnowledgeNode(
                kind="memory",
                id=memory.id,
                label=memory.original_text,
                attributes=memory.structured_data,
                provenance=memory.provenance,
                observed_at=memory.updated_at,
            )
            edges.append(
                KnowledgeEdge(
                    source=key,
                    predicate="APPLIES_TO",
                    target=conference_key,
                    provenance=memory.provenance,
                    observed_at=memory.updated_at,
                )
            )

        relations = list(
            await self.session.scalars(
                select(KnowledgeRelation).where(
                    KnowledgeRelation.user_id == user_id,
                    KnowledgeRelation.source_type == "conference",
                    KnowledgeRelation.source_id == conference_id,
                )
            )
        )
        for relation in relations:
            if relation.target_type != "conference" or relation.target_id not in accessible:
                continue
            target = await self.session.get(Conference, relation.target_id)
            if target is None:
                continue
            target_key = f"conference:{target.id}"
            nodes[target_key] = self._conference_node(target)
            edges.append(
                KnowledgeEdge(
                    source=conference_key,
                    predicate=relation.relation_type.value,
                    target=target_key,
                    provenance=relation.provenance,
                    observed_at=relation.created_at,
                )
            )
        return KnowledgeSubgraph(nodes=tuple(nodes.values()), edges=tuple(edges))

    async def related_conferences(
        self, user_id: int, conference_id: int, limit: int
    ) -> KnowledgeSubgraph:
        accessible = await self._accessible_conference_ids(user_id)
        if conference_id not in accessible or limit < 1:
            return KnowledgeSubgraph()
        relations = list(
            await self.session.scalars(
                select(KnowledgeRelation)
                .where(
                    KnowledgeRelation.user_id == user_id,
                    KnowledgeRelation.source_type == "conference",
                    KnowledgeRelation.source_id == conference_id,
                    KnowledgeRelation.target_type == "conference",
                )
                .order_by(KnowledgeRelation.id)
                .limit(limit)
            )
        )
        nodes: list[KnowledgeNode] = []
        edges: list[KnowledgeEdge] = []
        for relation in relations:
            if relation.target_id not in accessible:
                continue
            target = await self.session.get(Conference, relation.target_id)
            if target is None:
                continue
            nodes.append(self._conference_node(target))
            edges.append(
                KnowledgeEdge(
                    source=f"conference:{conference_id}",
                    predicate=relation.relation_type.value,
                    target=f"conference:{target.id}",
                    provenance=relation.provenance,
                    observed_at=relation.created_at,
                )
            )
        return KnowledgeSubgraph(nodes=tuple(nodes), edges=tuple(edges))

    async def _accessible_conference_ids(self, user_id: int) -> set[int]:
        by_topic = (
            select(ConferenceTopic.conference_id)
            .join(Topic, Topic.id == ConferenceTopic.topic_id)
            .where(Topic.user_id == user_id)
        )
        by_subscription = select(Subscription.conference_id).where(Subscription.user_id == user_id)
        return set(await self.session.scalars(union(by_topic, by_subscription)))

    @staticmethod
    def _conference_node(conference: Conference) -> KnowledgeNode:
        return KnowledgeNode(
            kind="conference",
            id=conference.id,
            label=conference.name,
            attributes={
                "website": conference.website,
                "start_date": conference.start_date,
                "end_date": conference.end_date,
                "location": conference.location,
            },
            provenance={"source_url": conference.source_url},
            observed_at=conference.updated_at,
        )

    @staticmethod
    def _search_text(conference: Conference) -> str:
        return " ".join(
            (conference.name, conference.description or "", conference.location or "")
        ).casefold()
