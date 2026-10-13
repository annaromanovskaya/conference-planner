from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conference_scout.schemas.common import MemoryKind, MemoryScope


class DomainReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    id: int
    position: int | None = None


class ConversationMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    content: str
    domain_references: tuple[DomainReference, ...] = ()


class MemoryProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: MemoryKind
    scope: MemoryScope
    original_text: str = Field(min_length=1)
    rule_data: dict[str, Any]
    provenance: dict[str, Any]
    topic_id: int | None = None
    conference_id: int | None = None

    @model_validator(mode="after")
    def validate_scope_link(self) -> "MemoryProposal":
        if self.scope == MemoryScope.TOPIC and self.topic_id is None:
            raise ValueError("topic-scoped memory requires topic_id")
        if self.scope == MemoryScope.CONFERENCE and self.conference_id is None:
            raise ValueError("conference-scoped memory requires conference_id")
        return self


class MemoryView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    kind: MemoryKind
    scope: MemoryScope
    original_text: str
    rule_data: dict[str, Any]
    topic_id: int | None = None
    conference_id: int | None = None
    provenance: dict[str, Any]


class MemoryConflict(BaseModel):
    model_config = ConfigDict(frozen=True)

    equivalent_ids: tuple[int, ...] = ()
    conflicting_ids: tuple[int, ...] = ()


class MemoryContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    conversation_id: int
    summary: str
    messages: tuple[ConversationMessage, ...]
    memories: tuple[MemoryView, ...]


class KnowledgeNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    id: int | str
    label: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime | None = None


class KnowledgeEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    predicate: str
    target: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime | None = None


class KnowledgeSubgraph(BaseModel):
    model_config = ConfigDict(frozen=True)

    nodes: tuple[KnowledgeNode, ...] = ()
    edges: tuple[KnowledgeEdge, ...] = ()
