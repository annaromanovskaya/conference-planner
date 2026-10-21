from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=1_000)
    url: HttpUrl
    snippet: str | None = Field(default=None, max_length=5_000)
    engine: str | None = Field(default=None, max_length=100)
