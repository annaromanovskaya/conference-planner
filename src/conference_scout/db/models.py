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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from conference_scout.db.base import Base, TimestampMixin, utc_now
from conference_scout.schemas.common import (
    ConferenceStatus,
    DeadlineType,
    FeedbackDecision,
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


JsonMapping = dict[str, Any]
