from datetime import date
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from conference_planner.schemas.common import (
    ArtifactValidationState,
    DeadlineType,
    EvidenceSourceClass,
    VerificationStatus,
)


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_url: HttpUrl
    source_class: EvidenceSourceClass
    excerpt: str = Field(min_length=1, max_length=1_000)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIABLE


class FactClaim(EvidenceClaim):
    kind: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=4_000)


class DeadlineClaim(EvidenceClaim):
    deadline_type: DeadlineType
    exact_date: date | None = None
    approximate_year: int | None = Field(default=None, ge=2000, le=2200)
    approximate_month: int | None = Field(default=None, ge=1, le=12)
    tentative: bool = False

    @model_validator(mode="after")
    def exact_or_approximate(self) -> Self:
        exact = self.exact_date is not None
        approximate = self.approximate_year is not None
        if exact == approximate:
            raise ValueError("provide exactly one of exact_date or approximate_year")
        if exact and self.approximate_month is not None:
            raise ValueError("exact deadline cannot also have approximate_month")
        return self


class ConferenceResearch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...] = Field(default=(), max_length=20)
    source_url: HttpUrl
    source_class: EvidenceSourceClass
    facts: tuple[FactClaim, ...] = Field(default=(), max_length=50)
    deadlines: tuple[DeadlineClaim, ...] = Field(default=(), max_length=50)


class ConferenceCatalogResearch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[ConferenceResearch, ...] = Field(default=(), max_length=50)


class VerifiedConferenceResearch(ConferenceResearch):
    verifier_notes: tuple[str, ...] = Field(default=(), max_length=20)


class EvidenceValidationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_type: str
    claim_index: int = Field(ge=0)
    state: ArtifactValidationState
    reason: str


class EvidenceValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validated: VerifiedConferenceResearch
    results: tuple[EvidenceValidationItem, ...]
