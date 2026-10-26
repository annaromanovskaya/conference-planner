from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.db.models import TrustedSource
from conference_planner.schemas.common import SourceType

_KNOWN_SOURCE_NAMES = {
    "https://www.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/en/meetings": (
        "International Astronomy Meetings — Canadian Astronomy Data Centre"
    ),
}


def source_display_name(source: TrustedSource) -> str:
    return friendly_source_name(source.url, source.name)


def friendly_source_name(url: str, stored_name: str | None = None) -> str:
    value = (stored_name or "").strip()
    if value and value.rstrip("/") != url.strip().rstrip("/") and not _is_url(value):
        return value

    normalized_url = url.strip().rstrip("/")
    known = _KNOWN_SOURCE_NAMES.get(normalized_url.casefold())
    if known is not None:
        return known

    parsed = urlsplit(url if "://" in url else f"https://{url}")
    hostname = (parsed.hostname or url).removeprefix("www.")
    path_label = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]).replace("-", " ")
    if path_label and path_label.casefold() not in {"index.html", "index.htm"}:
        return f"{hostname} — {path_label}"
    return hostname


def _is_url(value: str) -> bool:
    return urlsplit(value).scheme.casefold() in {"http", "https"}


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[TrustedSource]:
        return list(await self.session.scalars(select(TrustedSource).order_by(TrustedSource.name)))

    async def create(self, url: str, name: str, source_type: SourceType) -> TrustedSource:
        source = TrustedSource(
            url=url,
            name=friendly_source_name(url, name),
            source_type=source_type,
            enabled=True,
        )
        self.session.add(source)
        await self.session.flush()
        return source

    async def set_enabled(self, source_id: int, enabled: bool) -> None:
        source = await self.session.get(TrustedSource, source_id)
        if source is not None:
            source.enabled = enabled

    async def delete(self, source_id: int) -> None:
        source = await self.session.get(TrustedSource, source_id)
        if source is not None:
            await self.session.delete(source)
