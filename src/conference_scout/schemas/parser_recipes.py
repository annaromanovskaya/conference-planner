from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PageKind(StrEnum):
    DETAIL = "DETAIL"
    CATALOG = "CATALOG"


class RecipeStatus(StrEnum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"


class ParserRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str = Field(min_length=1, max_length=253)
    page_kind: PageKind
    card_selector: str | None = Field(default=None, max_length=200)
    field_selectors: dict[str, str] = Field(default_factory=dict)
    link_labels: tuple[str, ...] = ()
    regex_patterns: dict[str, str] = Field(default_factory=dict)

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        domain = value.strip().casefold().rstrip(".")
        if not domain or "://" in domain or "/" in domain:
            raise ValueError("domain must be a hostname")
        return domain


class RecipeLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    url: str


class RecipeCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    links: tuple[RecipeLink, ...] = ()
    regex_matches: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class RecipeExtraction(RecipeCard):
    cards: tuple[RecipeCard, ...] = ()
