from datetime import date

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from conference_planner.schemas.common import DeadlineType


class ExtractedDeadline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deadline_type: DeadlineType
    deadline_date: date | None = None
    source_url: HttpUrl | None = None

    @model_validator(mode="after")
    def require_source_for_date(self) -> "ExtractedDeadline":
        if self.deadline_date is not None and self.source_url is None:
            raise ValueError("a deadline date requires a source URL")
        return self


class ConferenceExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=500)
    acronym: str | None = Field(default=None, max_length=100)
    event_type: str | None = Field(default=None, max_length=100)
    start_date: date | None = None
    end_date: date | None = None
    location: str | None = Field(default=None, max_length=500)
    official_url: HttpUrl | None = None
    source_url: HttpUrl
    description: str | None = Field(default=None, max_length=5_000)
    deadlines: list[ExtractedDeadline] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_date_range(self) -> "ConferenceExtraction":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date cannot precede start_date")
        return self
