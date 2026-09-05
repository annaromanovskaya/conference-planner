import httpx
import pytest

from conference_scout.search.searxng import SearchProviderError, SearXNGSearchProvider


@pytest.mark.asyncio
async def test_searxng_normalizes_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "stars 2027 conference"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Stars 2027",
                        "url": "https://event.example/",
                        "content": "Scientific meeting",
                        "engine": "duckduckgo",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = SearXNGSearchProvider("http://searxng:8080", client)
        results = await provider.search("stars 2027 conference")

    assert results[0].title == "Stars 2027"
    assert str(results[0].url) == "https://event.example/"


@pytest.mark.asyncio
async def test_searxng_rejects_malformed_response() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"results": "bad"}))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SearchProviderError):
            await SearXNGSearchProvider("http://searxng:8080", client).search("query")
