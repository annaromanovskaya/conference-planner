from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from conference_planner.db.base import Base, TimestampMixin, utc_now
from conference_planner.schemas.common import (
    AgentRunStatus,
    AgentTaskStatus,
    AgentTaskType,
    ArtifactValidationState,
    CandidateDecisionReason,
    CandidateDecisionSource,
    CandidateStatus,
    ConferenceStatus,
    ConversationStatus,
    DeadlineType,
    EvidenceSourceClass,
    FeedbackDecision,
    KnowledgeRelationKind,
    MemoryKind,
    MemoryScope,
    MonitoringAttemptStatus,
    PendingActionStatus,
    ProgressEventType,
    ProviderUsageStatus,
    SourceType,
    TestReminderStatus,
    VerificationStatus,
    WorkflowKind,
)
from conference_planner.schemas.parser_recipes import PageKind, RecipeStatus


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, _dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class Topic(TimestampMixin, Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class TrustedSource(Base):
    __tablename__ = "trusted_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2_048), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conference(TimestampMixin, Base):
    __tablename__ = "conferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    normalized_name: Mapped[str] = mapped_column(String(500), index=True)
    acronym: Mapped[str | None] = mapped_column(String(100))
    event_type: Mapped[str | None] = mapped_column(String(100))
    website: Mapped[str | None] = mapped_column(String(2_048), unique=True)
    source_url: Mapped[str | None] = mapped_column(String(2_048))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ConferenceStatus] = mapped_column(
        Enum(ConferenceStatus), default=ConferenceStatus.DISCOVERED, index=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    is_catalog_source: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), index=True
    )

    deadlines: Mapped[list["Deadline"]] = relationship(
        back_populates="conference", cascade="all, delete-orphan"
    )


class ConferenceTopic(Base):
    __tablename__ = "conference_topics"
    __table_args__ = (UniqueConstraint("conference_id", "topic_id"),)

    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )
    relevance_score: Mapped[float] = mapped_column(Float)


class Deadline(TimestampMixin, Base):
    __tablename__ = "deadlines"
    __table_args__ = (
        UniqueConstraint("conference_id", "deadline_type"),
        CheckConstraint(
            "(deadline_date IS NOT NULL AND approximate_year IS NULL "
            "AND approximate_month IS NULL) OR "
            "(deadline_date IS NULL AND approximate_year IS NOT NULL)",
            name="deadline_exact_or_approximate",
        ),
        CheckConstraint(
            "approximate_month IS NULL OR (approximate_month >= 1 AND approximate_month <= 12)",
            name="deadline_approximate_month",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE"), index=True
    )
    deadline_type: Mapped[DeadlineType] = mapped_column(Enum(DeadlineType))
    deadline_date: Mapped[date | None] = mapped_column(Date)
    approximate_year: Mapped[int | None]
    approximate_month: Mapped[int | None]
    tentative: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[str] = mapped_column(String(2_048))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus), default=VerificationStatus.SECONDARY_UNVERIFIED
    )
    source_class: Mapped[EvidenceSourceClass] = mapped_column(
        Enum(EvidenceSourceClass), default=EvidenceSourceClass.SECONDARY
    )
    evidence_excerpt: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True), default=utc_now)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    conference: Mapped[Conference] = relationship(back_populates="deadlines")


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "conference_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    conference_id: Mapped[int] = mapped_column(ForeignKey("conferences.id", ondelete="CASCADE"))
    status: Mapped[ConferenceStatus] = mapped_column(
        Enum(ConferenceStatus), default=ConferenceStatus.TRACKING
    )
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "deadline_id",
            "notification_kind",
            "threshold",
            name="uq_reminders_delivery_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    deadline_id: Mapped[int] = mapped_column(ForeignKey("deadlines.id", ondelete="CASCADE"))
    days_before: Mapped[int]
    notification_kind: Mapped[str] = mapped_column(String(32), default="EXACT")
    threshold: Mapped[int] = mapped_column(default=0)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class TestReminderTask(TimestampMixin, Base):
    __tablename__ = "test_reminder_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE"), unique=True
    )
    deadline_id: Mapped[int] = mapped_column(
        ForeignKey("deadlines.id", ondelete="CASCADE"), unique=True
    )
    send_at: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True), index=True)
    status: Mapped[TestReminderStatus] = mapped_column(
        Enum(TestReminderStatus), default=TestReminderStatus.PENDING, index=True
    )
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class DeadlineHistory(Base):
    __tablename__ = "deadline_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    conference_id: Mapped[int] = mapped_column(ForeignKey("conferences.id", ondelete="CASCADE"))
    deadline_type: Mapped[DeadlineType] = mapped_column(Enum(DeadlineType))
    old_date: Mapped[date] = mapped_column(Date)
    new_date: Mapped[date] = mapped_column(Date)
    detected_at: Mapped[datetime] = mapped_column(default=utc_now)
    source_url: Mapped[str] = mapped_column(String(2_048))


class ConferenceAlias(Base):
    __tablename__ = "conference_aliases"
    __table_args__ = (UniqueConstraint("conference_id", "normalized_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str] = mapped_column(String(500))
    normalized_alias: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class ConferenceEvidence(Base):
    __tablename__ = "conference_evidence"
    __table_args__ = (UniqueConstraint("conference_id", "fact_kind", "fact_value", "source_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE"), index=True
    )
    fact_kind: Mapped[str] = mapped_column(String(100), index=True)
    fact_value: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(2_048))
    source_class: Mapped[EvidenceSourceClass] = mapped_column(Enum(EvidenceSourceClass))
    excerpt: Mapped[str] = mapped_column(Text)
    verification_status: Mapped[VerificationStatus] = mapped_column(Enum(VerificationStatus))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class EventCandidate(TimestampMixin, Base):
    __tablename__ = "event_candidates"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source_url",
            "normalized_title",
            name="uq_event_candidates_user_source_title",
        ),
        Index("ix_event_candidates_user_status", "user_id", "status"),
        Index("ix_event_candidates_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(500))
    normalized_title: Mapped[str] = mapped_column(String(500), index=True)
    source_url: Mapped[str] = mapped_column(String(2_048))
    query: Mapped[str] = mapped_column(String(1_000))
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    summary: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[VerificationStatus] = mapped_column(Enum(VerificationStatus))
    status: Mapped[CandidateStatus] = mapped_column(
        Enum(CandidateStatus), default=CandidateStatus.ACTIVE
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(500))
    official_url: Mapped[str | None] = mapped_column(String(2_048))
    research_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    researched_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    topic_relevance_score: Mapped[float | None] = mapped_column(Float)
    topic_relevance_reason: Mapped[str | None] = mapped_column(Text)
    decision_reason: Mapped[CandidateDecisionReason | None] = mapped_column(
        Enum(CandidateDecisionReason)
    )
    decision_source: Mapped[CandidateDecisionSource | None] = mapped_column(
        Enum(CandidateDecisionSource)
    )
    decision_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    next_review_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))


class ConferenceChange(Base):
    __tablename__ = "conference_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE"), index=True
    )
    field_name: Mapped[str] = mapped_column(String(100), index=True)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(2_048))
    verification_status: Mapped[VerificationStatus] = mapped_column(Enum(VerificationStatus))
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True), default=utc_now)


class MonitoringAttempt(Base):
    __tablename__ = "monitoring_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[MonitoringAttemptStatus] = mapped_column(
        Enum(MonitoringAttemptStatus), index=True
    )
    source_url: Mapped[str | None] = mapped_column(String(2_048))
    error_code: Mapped[str | None] = mapped_column(String(100))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True), default=utc_now)


class WebDocumentCache(TimestampMixin, Base):
    __tablename__ = "web_document_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_url: Mapped[str] = mapped_column(String(2_048), unique=True, index=True)
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    extracted_text: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(default=200)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True), default=utc_now)


class SiteParserRecipe(TimestampMixin, Base):
    __tablename__ = "site_parser_recipes"
    __table_args__ = (
        UniqueConstraint("domain", "page_kind", "version"),
        Index("ix_site_parser_recipes_lookup", "domain", "page_kind", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(253))
    page_kind: Mapped[PageKind] = mapped_column(Enum(PageKind))
    version: Mapped[int]
    status: Mapped[RecipeStatus] = mapped_column(Enum(RecipeStatus), index=True)
    card_selector: Mapped[str | None] = mapped_column(String(200))
    field_selectors: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    link_labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    regex_patterns: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    failure_count: Mapped[int] = mapped_column(default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100))


class ConferenceFeedback(Base):
    __tablename__ = "conference_feedback"
    __table_args__ = (UniqueConstraint("user_id", "conference_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    conference_id: Mapped[int] = mapped_column(ForeignKey("conferences.id", ondelete="CASCADE"))
    decision: Mapped[FeedbackDecision] = mapped_column(Enum(FeedbackDecision))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class ModelSelection(TimestampMixin, Base):
    __tablename__ = "model_selections"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    profile_id: Mapped[str] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(300))


class ModelCapability(Base):
    __tablename__ = "model_capabilities"
    __table_args__ = (UniqueConstraint("profile_id", "model_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(100), index=True)
    model_name: Mapped[str] = mapped_column(String(300))
    supports_tools: Mapped[bool] = mapped_column(Boolean)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class OllamaUsageDaily(TimestampMixin, Base):
    __tablename__ = "ollama_usage_daily"
    __table_args__ = (UniqueConstraint("user_id", "profile_id", "model_name", "usage_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[str] = mapped_column(String(100), index=True)
    model_name: Mapped[str] = mapped_column(String(300), index=True)
    usage_date: Mapped[date] = mapped_column(Date, index=True)
    request_count: Mapped[int] = mapped_column(BigInteger, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)


class ProviderUsageState(Base):
    __tablename__ = "provider_usage_status"
    __table_args__ = (UniqueConstraint("user_id", "profile_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[ProviderUsageStatus] = mapped_column(Enum(ProviderUsageStatus))
    model_name: Mapped[str | None] = mapped_column(String(300))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(timezone=True))
    detail_code: Mapped[str | None] = mapped_column(String(100))


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index(
            "uq_conversations_active_user",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[ConversationStatus] = mapped_column(
        Enum(ConversationStatus), default=ConversationStatus.ACTIVE, index=True
    )
    summary: Mapped[str] = mapped_column(Text, default="")


class ChatMessageRecord(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    domain_references: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class UserMemory(TimestampMixin, Base):
    __tablename__ = "user_memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[MemoryKind] = mapped_column(Enum(MemoryKind), index=True)
    scope: Mapped[MemoryScope] = mapped_column(Enum(MemoryScope), index=True)
    original_text: Mapped[str] = mapped_column(Text)
    structured_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    conference_id: Mapped[int | None] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE")
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PendingAction(TimestampMixin, Base):
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[PendingActionStatus] = mapped_column(
        Enum(PendingActionStatus), default=PendingActionStatus.PENDING, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    error: Mapped[str | None] = mapped_column(Text)


class KnowledgeRelation(Base):
    __tablename__ = "knowledge_relations"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source_type",
            "source_id",
            "relation_type",
            "target_type",
            "target_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(50), index=True)
    source_id: Mapped[int] = mapped_column(index=True)
    relation_type: Mapped[KnowledgeRelationKind] = mapped_column(
        Enum(KnowledgeRelationKind), index=True
    )
    target_type: Mapped[str] = mapped_column(String(50), index=True)
    target_id: Mapped[int] = mapped_column(index=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    request_text: Mapped[str] = mapped_column(Text)
    workflow_kind: Mapped[WorkflowKind] = mapped_column(
        Enum(WorkflowKind), default=WorkflowKind.CONFERENCE_RESEARCH, index=True
    )
    status: Mapped[AgentRunStatus] = mapped_column(Enum(AgentRunStatus), index=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))


class AgentTask(TimestampMixin, Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "task_key"),
        Index("ix_agent_tasks_run_status", "run_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    task_key: Mapped[str] = mapped_column(String(100))
    task_type: Mapped[AgentTaskType] = mapped_column(Enum(AgentTaskType))
    status: Mapped[AgentTaskStatus] = mapped_column(Enum(AgentTaskStatus), index=True)
    dependencies: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(default=0)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(100))


class AgentArtifact(Base):
    __tablename__ = "agent_artifacts"
    __table_args__ = (Index("ix_agent_artifacts_run_kind", "run_id", "artifact_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    task_id: Mapped[int] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"))
    artifact_kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    validation_state: Mapped[ArtifactValidationState] = mapped_column(
        Enum(ArtifactValidationState), default=ArtifactValidationState.UNVALIDATED
    )
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class AgentProgressEvent(Base):
    __tablename__ = "agent_progress_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        Index("ix_agent_progress_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    sequence: Mapped[int]
    event_type: Mapped[ProgressEventType] = mapped_column(Enum(ProgressEventType))
    message: Mapped[str] = mapped_column(Text)
    safe_data: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


JsonMapping = dict[str, Any]


class DiscoveryJob(Base):
    """Durable Telegram-facing queue around an agent workflow."""
    __tablename__ = "discovery_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    years: Mapped[list[int]] = mapped_column(JSON)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="QUEUED")
    stage: Mapped[str] = mapped_column(String(50), default="QUEUED")
    processed: Mapped[int] = mapped_column(default=0)
    total: Mapped[int | None] = mapped_column(nullable=True)
    found: Mapped[int] = mapped_column(default=0)
    rejected: Mapped[int] = mapped_column(default=0)
    errors: Mapped[int] = mapped_column(default=0)
    agent_run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(nullable=True)
    report_message_id: Mapped[int | None] = mapped_column(nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


class DiscoverySearchAudit(Base):
    """Immutable per-search-request evidence for agent discovery."""
    __tablename__ = "discovery_search_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("discovery_jobs.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(Text)
    page: Mapped[int] = mapped_column(default=1)
    result_count: Mapped[int] = mapped_column(default=0)
    retained_count: Mapped[int] = mapped_column(default=0)
    ignored_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(30))
    diagnostics: Mapped[list[str]] = mapped_column(JSON, default=list)
    urls: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class DiscoveryURLTask(Base):
    """Persistent unit of a source crawl. The worker leases one URL at a time."""
    __tablename__ = "discovery_url_tasks"
    __table_args__ = (UniqueConstraint("job_id", "normalized_url", "task_type", name="uq_discovery_url_task"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("discovery_jobs.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    normalized_url: Mapped[str] = mapped_column(String(2048))
    parent_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("event_candidates.id", ondelete="SET NULL"), nullable=True)
    root_url: Mapped[str] = mapped_column(String(2048))
    label: Mapped[str] = mapped_column(String(300), default="")
    task_type: Mapped[str] = mapped_column(String(30), default="CATALOG")
    depth: Mapped[int] = mapped_column(default=0)
    priority: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)
