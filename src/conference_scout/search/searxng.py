import httpx
from pydantic import TypeAdapter, ValidationError

from conference_scout.schemas.integrations import SearchResult


class SearchProviderError(RuntimeError):
    pass


class SearXNGSearchProvider:
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
        if page < 1:
            raise ValueError("search page must be positive")
        try:
            response = await self.client.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "pageno": page},
            )
            response.raise_for_status()
            payload = response.json()
            raw_results = payload.get("results")
            if not isinstance(raw_results, list):
                raise SearchProviderError("SearXNG response has no result list")
            normalized = [
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "snippet": item.get("content"),
                    "engine": item.get("engine"),
                }
                for item in raw_results
                if isinstance(item, dict)
            ]
            return TypeAdapter(list[SearchResult]).validate_python(normalized)
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            raise SearchProviderError("SearXNG request failed") from exc
