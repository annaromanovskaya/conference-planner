from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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

from conference_scout.db.base import Base, TimestampMixin, utc_now
from conference_scout.schemas.common import (
    ConferenceStatus,
    ConversationStatus,
    DeadlineType,
    FeedbackDecision,
    KnowledgeRelationKind,
    MemoryKind,
    MemoryScope,
    PendingActionStatus,
    SourceType,
)


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
    __table_args__ = (UniqueConstraint("conference_id", "deadline_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE"), index=True
    )
    deadline_type: Mapped[DeadlineType] = mapped_column(Enum(DeadlineType))
    deadline_date: Mapped[date] = mapped_column(Date)
    source_url: Mapped[str] = mapped_column(String(2_048))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
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
    __table_args__ = (UniqueConstraint("user_id", "deadline_id", "days_before"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    deadline_id: Mapped[int] = mapped_column(ForeignKey("deadlines.id", ondelete="CASCADE"))
    days_before: Mapped[int]
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class DeadlineHistory(Base):
    __tablename__ = "deadline_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    conference_id: Mapped[int] = mapped_column(ForeignKey("conferences.id", ondelete="CASCADE"))
    deadline_type: Mapped[DeadlineType] = mapped_column(Enum(DeadlineType))
    old_date: Mapped[date] = mapped_column(Date)
    new_date: Mapped[date] = mapped_column(Date)
    detected_at: Mapped[datetime] = mapped_column(default=utc_now)
    source_url: Mapped[str] = mapped_column(String(2_048))


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


JsonMapping = dict[str, Any]
