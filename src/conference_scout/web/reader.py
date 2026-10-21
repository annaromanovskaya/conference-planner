from urllib.parse import urlsplit

import httpx


class ReaderError(RuntimeError):
    pass


class JinaPageReader:
    """Read a public page through Jina Reader after a direct fetch failure."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str = "https://r.jina.ai",
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")

    async def read(self, url: str, *, max_bytes: int) -> str:
        parsed = urlsplit(url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise ReaderError("reader target must be a public HTTP URL")
        try:
            async with self.client.stream(
                "GET",
                f"{self.base_url}/{url}",
                headers={"Accept": "text/plain"},
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if not any(value in content_type for value in ("text/", "application/json")):
                    raise ReaderError("reader response is not text")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ReaderError("reader response exceeds maximum size")
                text = bytes(body).decode(response.encoding or "utf-8", errors="replace").strip()
                if not text:
                    raise ReaderError("reader returned empty text")
                return text
        except ReaderError:
            raise
        except (httpx.HTTPError, UnicodeError) as exc:
            raise ReaderError("reader request failed") from exc
