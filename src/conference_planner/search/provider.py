from dataclasses import dataclass
from typing import Protocol

from conference_planner.schemas.integrations import SearchResult


@dataclass(frozen=True, slots=True)
class SearchPage:
    results: tuple[SearchResult, ...]
    unresponsive_engines: tuple[str, ...] = ()


class SearchProvider(Protocol):
    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]: ...
