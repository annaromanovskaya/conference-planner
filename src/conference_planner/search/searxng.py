import httpx
from pydantic import TypeAdapter, ValidationError

from conference_planner.schemas.integrations import SearchResult
from conference_planner.search.provider import SearchPage


class SearchProviderError(RuntimeError):
    pass


class SearXNGSearchProvider:
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
        return list((await self.search_page(query, page=page)).results)

    async def search_page(self, query: str, *, page: int = 1) -> SearchPage:
        if page < 1:
            raise ValueError("search page must be positive")
        try:
            response = await self.client.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "pageno": page},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise SearchProviderError("SearXNG response is not an object")
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
            results = TypeAdapter(list[SearchResult]).validate_python(normalized)
            diagnostics = self._engine_diagnostics(payload.get("unresponsive_engines"))
            return SearchPage(tuple(results), diagnostics)
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            raise SearchProviderError("SearXNG request failed") from exc

    @staticmethod
    def _engine_diagnostics(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        diagnostics: list[str] = []
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            engine, reason = item[0], item[1]
            if not isinstance(engine, str) or not isinstance(reason, str):
                continue
            engine = engine.strip()[:80]
            reason = reason.strip()[:120]
            if engine and reason:
                diagnostics.append(f"{engine}: {reason}")
        return tuple(dict.fromkeys(diagnostics))
