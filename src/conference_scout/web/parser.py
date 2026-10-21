from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from conference_scout.schemas.parser_recipes import RecipeExtraction, RecipeLink


@dataclass(frozen=True, slots=True)
class PageLink:
    text: str
    url: str


def extract_page_links(html: str, base_url: str, *, max_links: int = 200) -> tuple[PageLink, ...]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[PageLink] = []
    seen: set[tuple[str, str]] = set()
    for node in soup.find_all("a", href=True):
        raw_href = node.get("href")
        if not isinstance(raw_href, str) or not raw_href.strip():
            continue
        resolved, fragment = urldefrag(urljoin(base_url, raw_href.strip()))
        parsed = urlsplit(resolved)
        if fragment and resolved.rstrip("/") == base_url.rstrip("/"):
            continue
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            continue
        label = " ".join(node.get_text(" ", strip=True).split())
        if not label:
            fallback = node.get("aria-label") or node.get("title")
            label = " ".join(str(fallback or "").split())
        if not label:
            continue
        identity = (label.casefold(), resolved)
        if identity in seen:
            continue
        seen.add(identity)
        links.append(PageLink(text=label, url=resolved))
        if len(links) >= max_links:
            break
    return tuple(links)


def extract_page_text(html: str, *, max_chars: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    metadata: list[str] = []
    supported_metadata = {
        "description",
        "og:title",
        "og:description",
        "twitter:title",
        "twitter:description",
    }
    for node in soup.find_all("meta"):
        key = str(node.get("property") or node.get("name") or "").casefold()
        content = node.get("content")
        if key in supported_metadata and isinstance(content, str) and content.strip():
            metadata.append(content.strip())
    for node in soup(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        node.decompose()
    for node in soup(["input", "button", "select", "textarea"]):
        node.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    text = "\n".join(dict.fromkeys((*metadata, *(line for line in lines if line))))
    return text[:max_chars]


def extract_structured_page(
    html: str,
    base_url: str,
    *,
    max_chars: int,
    max_links: int = 200,
) -> RecipeExtraction:
    text = extract_page_text(html, max_chars=max_chars)
    links = tuple(
        RecipeLink(text=item.text, url=item.url)
        for item in extract_page_links(html, base_url, max_links=max_links)
    )
    return RecipeExtraction(fields={"text": (text,) if text else ()}, links=links)
