from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, model_validator

from conference_planner.schemas.common import (
    DeadlineType,
    MemoryKind,
    MemoryScope,
    PendingActionStatus,
    SourceType,
)


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


class AddTopicAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["add_topic"] = "add_topic"
    name: str = Field(min_length=1)
    description: str = ""
    keywords: tuple[str, ...] = ()

    def confirmation_summary(self) -> str:
        return f"Добавить тему «{self.name}»"


class DeleteTopicAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["delete_topic"] = "delete_topic"
    topic_id: int = Field(gt=0)
    name: str = Field(min_length=1)

    def confirmation_summary(self) -> str:
        return f"Удалить тему «{self.name}»"


class AddSourceAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["add_source"] = "add_source"
    source_type: SourceType
    value: str = Field(min_length=1)

    def confirmation_summary(self) -> str:
        return f"Добавить источник {self.value}"


class DeleteSourceAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["delete_source"] = "delete_source"
    source_id: int = Field(gt=0)
    name: str = Field(min_length=1)

    def confirmation_summary(self) -> str:
        return f"Удалить источник «{self.name}»"


class TrackConferenceAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["track_conference"] = "track_conference"
    conference_id: int = Field(gt=0)

    def confirmation_summary(self) -> str:
        return f"Отслеживать конференцию {self.conference_id}"


class DeleteConferenceAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["delete_conference"] = "delete_conference"
    conference_id: int = Field(gt=0)
    name: str = Field(min_length=1)

    def confirmation_summary(self) -> str:
        return f"Удалить конференцию «{self.name}» из моих списков"


class ImportConferenceCandidateAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["import_conference_candidate"] = "import_conference_candidate"
    candidate_id: int = Field(gt=0)

    def confirmation_summary(self) -> str:
        return f"Проверить, сохранить и отслеживать кандидата {self.candidate_id}"


class ImportConferenceURLAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["import_conference_url"] = "import_conference_url"
    title: str = Field(min_length=1, max_length=500)
    source_url: HttpUrl

    def confirmation_summary(self) -> str:
        return f"Проверить, сохранить и отслеживать «{self.title}»"


class ArchiveConferenceAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["archive_conference"] = "archive_conference"
    conference_id: int = Field(gt=0)

    def confirmation_summary(self) -> str:
        return f"Перенести в архив конференцию {self.conference_id}"


class SaveMemoryAction(MemoryProposal):
    action: Literal["save_memory"] = "save_memory"

    def confirmation_summary(self) -> str:
        return f"Запомнить: {self.original_text}"


class ForgetMemoryAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["forget_memory"] = "forget_memory"
    memory_id: int = Field(gt=0)

    def confirmation_summary(self) -> str:
        return f"Забыть запись памяти {self.memory_id}"


class RunDiscoveryAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["run_discovery"] = "run_discovery"
    years: tuple[int, ...] = Field(min_length=1)

    def confirmation_summary(self) -> str:
        years = ", ".join(str(year) for year in self.years)
        return f"Запустить поиск конференций за {years}"


class SetCandidateDecisionAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["set_candidate_decision"] = "set_candidate_decision"
    candidate_id: int = Field(gt=0)
    decision: Literal["ACTIVE", "SKIPPED", "REJECTED"]

    def confirmation_summary(self) -> str:
        labels = {
            "ACTIVE": "Вернуть кандидата в очередь",
            "SKIPPED": "Пропустить кандидата",
            "REJECTED": "Отклонить кандидата",
        }
        return f"{labels[self.decision]} {self.candidate_id}"


class UpsertDeadlineAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["upsert_deadline"] = "upsert_deadline"
    conference_id: int = Field(gt=0)
    deadline_type: DeadlineType
    exact_date: date | None = None
    approximate_year: int | None = Field(default=None, ge=2000, le=2200)
    approximate_month: int | None = Field(default=None, ge=1, le=12)
    tentative: bool = True
    source_url: HttpUrl | None = None

    @model_validator(mode="after")
    def exact_or_approximate(self) -> "UpsertDeadlineAction":
        exact = self.exact_date is not None
        approximate = self.approximate_year is not None
        if exact == approximate:
            raise ValueError("provide exactly one of exact_date or approximate_year")
        if exact and self.approximate_month is not None:
            raise ValueError("exact deadline cannot also have approximate_month")
        return self

    def confirmation_summary(self) -> str:
        if self.exact_date is not None:
            when = self.exact_date.isoformat()
        else:
            when = str(self.approximate_year)
            if self.approximate_month is not None:
                when += f"-{self.approximate_month:02d}"
            when += " (ориентировочно)"
        return (
            f"Записать дедлайн {self.deadline_type.value} для конференции "
            f"{self.conference_id}: {when}"
        )


ActionPayload = Annotated[
    AddTopicAction
    | DeleteTopicAction
    | AddSourceAction
    | DeleteSourceAction
    | TrackConferenceAction
    | DeleteConferenceAction
    | ImportConferenceCandidateAction
    | ImportConferenceURLAction
    | ArchiveConferenceAction
    | SaveMemoryAction
    | ForgetMemoryAction
    | RunDiscoveryAction
    | SetCandidateDecisionAction
    | UpsertDeadlineAction,
    Field(discriminator="action"),
]

ACTION_PAYLOAD_ADAPTER = TypeAdapter(ActionPayload)


def parse_action_payload(data: Any) -> ActionPayload:
    return ACTION_PAYLOAD_ADAPTER.validate_python(data)


class PendingActionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    action_type: str
    summary: str
    status: PendingActionStatus
    expires_at: datetime


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_id: int
    success: bool
    message: str
