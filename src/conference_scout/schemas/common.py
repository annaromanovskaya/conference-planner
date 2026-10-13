from enum import StrEnum


class ConferenceStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    TRACKING = "TRACKING"
    ARCHIVED = "ARCHIVED"


class DeadlineType(StrEnum):
    ABSTRACT_SUBMISSION = "ABSTRACT_SUBMISSION"
    REGISTRATION = "REGISTRATION"
    EARLY_REGISTRATION = "EARLY_REGISTRATION"
    OTHER = "OTHER"


class SourceType(StrEnum):
    DOMAIN = "DOMAIN"
    PAGE = "PAGE"


class FeedbackDecision(StrEnum):
    NOT_INTERESTED = "NOT_INTERESTED"


class ConversationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class MemoryKind(StrEnum):
    NOTE = "NOTE"
    PREFERENCE = "PREFERENCE"
    INSTRUCTION = "INSTRUCTION"


class MemoryScope(StrEnum):
    GLOBAL = "GLOBAL"
    TOPIC = "TOPIC"
    CONFERENCE = "CONFERENCE"


class PendingActionStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class KnowledgeRelationKind(StrEnum):
    SAME_AS = "SAME_AS"
    RELATED_TO = "RELATED_TO"
