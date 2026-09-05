from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from conference_scout.web.url_safety import URLSafetyPolicy


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    html: str


class PageFetcher:
    def __init__(self, client: httpx.AsyncClient, safety: URLSafetyPolicy) -> None:
        self.client = client
        self.safety = safety

    async def fetch(self, url: str, *, max_bytes: int, max_redirects: int = 5) -> FetchedPage:
        current = await self.safety.validate(url)
        try:
            for _ in range(max_redirects + 1):
                async with self.client.stream("GET", current, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("redirect has no location")
                        current = await self.safety.validate(urljoin(current, location))
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if (
                        "text/html" not in content_type
                        and "application/xhtml+xml" not in content_type
                    ):
                        raise FetchError("response is not HTML")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise FetchError("response exceeds maximum size")
                    encoding = response.encoding or "utf-8"
                    return FetchedPage(current, bytes(body).decode(encoding, errors="replace"))
            raise FetchError("too many redirects")
        except httpx.HTTPError as exc:
            raise FetchError("page request failed") from exc
