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
