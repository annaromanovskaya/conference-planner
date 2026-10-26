from urllib.parse import urlsplit


def is_official_source(source_url: str, official_url: str | None) -> bool:
    if not official_url:
        return False
    return (urlsplit(source_url).hostname or "").casefold() == (
        urlsplit(official_url).hostname or ""
    ).casefold()
