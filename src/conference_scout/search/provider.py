from typing import Protocol

from conference_scout.schemas.integrations import SearchResult


class SearchProvider(Protocol):
    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]: ...
