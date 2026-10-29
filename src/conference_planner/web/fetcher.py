import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from conference_planner.web.access import (
    AccessStatus,
    EvidenceOrigin,
    PageDiagnostics,
    looks_like_access_challenge,
)
from conference_planner.web.url_safety import URLSafetyPolicy


class FetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        user_message: str | None = None,
        url: str | None = None,
        http_status: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or type(self).__name__
        self.url = url
        self.http_status = http_status
        self.retry_after = retry_after
        self.user_message = user_message or "Сайт не ответил корректной HTML-страницей."


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    html: str
    status_code: int = 200
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str = ""
    not_modified: bool = False
    diagnostics: PageDiagnostics | None = None


def normalized_content_hash(html: str) -> str:
    normalized = re.sub(r"\s+", " ", html).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class PageFetcher:
    def __init__(self, client: httpx.AsyncClient, safety: URLSafetyPolicy) -> None:
        self.client = client
        self.safety = safety

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        max_redirects: int = 5,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchedPage:
        current = await self.safety.validate(url)
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/129.0 Safari/537.36 ConfPlanner/0.1"
            ),
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            for _ in range(max_redirects + 1):
                async with self.client.stream(
                    "GET",
                    current,
                    follow_redirects=False,
                    headers=headers,
                ) as response:
                    if response.status_code == 304:
                        return FetchedPage(
                            current,
                            "",
                            status_code=304,
                            etag=response.headers.get("etag", etag),
                            last_modified=response.headers.get("last-modified", last_modified),
                            not_modified=True,
                            diagnostics=PageDiagnostics(current, AccessStatus.NOT_MODIFIED, EvidenceOrigin.VALIDATED_CACHE, 304),
                        )
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError(
                                "redirect has no location",
                                code="REDIRECT_NO_LOCATION",
                                user_message="Сайт вернул некорректное перенаправление.",
                            )
                        current = await self.safety.validate(urljoin(current, location))
                        continue
                    if response.status_code in (403, 429):
                        raise FetchError(
                            f"access restricted (HTTP {response.status_code})",
                            code="ACCESS_DENIED" if response.status_code == 403 else "RATE_LIMITED",
                            user_message=(
                                "Сайт ограничил автоматический доступ (HTTP 403)."
                                if response.status_code == 403 else
                                "Сайт ограничил частоту обращений (HTTP 429)."
                            ),
                            url=current,
                            http_status=response.status_code,
                            retry_after=response.headers.get("retry-after"),
                        )
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if (
                        "text/html" not in content_type
                        and "application/xhtml+xml" not in content_type
                    ):
                        raise FetchError(
                            "response is not HTML",
                            code="NOT_HTML",
                            user_message="Ссылка ведёт не на HTML-страницу.",
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise FetchError(
                                "response exceeds maximum size",
                                code="PAGE_TOO_LARGE",
                                user_message="Страница слишком большая для безопасной обработки.",
                            )
                    encoding = response.encoding or "utf-8"
                    html = bytes(body).decode(encoding, errors="replace")
                    if looks_like_access_challenge(html):
                        raise FetchError(
                            "human verification challenge detected",
                            code="CAPTCHA_REQUIRED",
                            user_message="Сайт требует проверки человека (CAPTCHA).",
                            url=current,
                            http_status=response.status_code,
                        )
                    return FetchedPage(
                        current,
                        html,
                        status_code=response.status_code,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        content_hash=normalized_content_hash(html),
                        diagnostics=PageDiagnostics(current, AccessStatus.AVAILABLE, EvidenceOrigin.LIVE_PAGE, response.status_code),
                    )
            raise FetchError(
                "too many redirects",
                code="TOO_MANY_REDIRECTS",
                user_message="Сайт создал слишком много перенаправлений.",
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise FetchError(
                "page returned an HTTP error",
                code=f"HTTP_{status}",
                user_message=f"Сайт вернул HTTP {status}.",
                url=str(exc.request.url),
                http_status=status,
            ) from exc
        except httpx.TimeoutException as exc:
            raise FetchError(
                "page request timed out",
                code="TIMEOUT",
                user_message="Сайт не ответил за отведённое время.",
                url=str(exc.request.url),
            ) from exc
        except httpx.RequestError as exc:
            raise FetchError(
                "page request failed",
                code="NETWORK_ERROR",
                user_message="Не удалось установить соединение с сайтом.",
                url=str(exc.request.url),
            ) from exc
