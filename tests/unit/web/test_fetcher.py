import ipaddress

import httpx
import pytest

from conference_scout.web.fetcher import FetchError, PageFetcher
from conference_scout.web.url_safety import URLSafetyPolicy


async def public_resolver(_host: str, _port: int):
    return [ipaddress.ip_address("93.184.216.34")]


@pytest.mark.asyncio
async def test_fetcher_returns_bounded_html() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "text/html"}, text="<h1>Event</h1>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        page = await PageFetcher(client, URLSafetyPolicy(public_resolver)).fetch(
            "https://event.example", max_bytes=100
        )
    assert page.url == "https://event.example/"
    assert page.html == "<h1>Event</h1>"


@pytest.mark.asyncio
async def test_fetcher_rejects_non_html() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"pdf")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FetchError):
            await PageFetcher(client, URLSafetyPolicy(public_resolver)).fetch(
                "https://event.example", max_bytes=100
            )
