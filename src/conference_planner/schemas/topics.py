from pydantic import BaseModel, ConfigDict, Field, field_validator


class TopicNormalization(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2_000)
    keywords: list[str] = Field(min_length=1, max_length=20)

    @field_validator("keywords")
    @classmethod
    def clean_keywords(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not cleaned:
            raise ValueError("at least one non-empty keyword is required")
        return cleaned


class RelevanceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    score: float = Field(ge=0, le=1)
    explanation: str = Field(min_length=1, max_length=1_000)
