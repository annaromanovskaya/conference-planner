import re
from collections.abc import Iterable
from urllib.parse import urldefrag, urljoin, urlsplit

import soupsieve
from bs4 import BeautifulSoup, Tag

from conference_planner.schemas.parser_recipes import (
    PageKind,
    ParserRecipe,
    RecipeCard,
    RecipeExtraction,
    RecipeLink,
)

ALLOWED_FIELDS = frozenset(
    {
        "name",
        "acronym",
        "event_dates",
        "deadlines",
        "location",
        "description",
        "organizer",
        "registration",
        "text",
    }
)
MAX_HTML_CHARS = 2_000_000
MAX_RESULTS_PER_FIELD = 100


class RecipeValidationError(ValueError):
    pass


def validate_recipe(recipe: ParserRecipe) -> None:
    unknown = set(recipe.field_selectors) - ALLOWED_FIELDS
    if unknown:
        raise RecipeValidationError(f"unknown recipe fields: {', '.join(sorted(unknown))}")
    if recipe.page_kind == PageKind.CATALOG and not recipe.card_selector:
        raise RecipeValidationError("catalog recipe requires card_selector")
    selectors = [*recipe.field_selectors.values()]
    if recipe.card_selector:
        selectors.append(recipe.card_selector)
    for selector in selectors:
        _validate_selector(selector)
    if len(recipe.link_labels) > 50:
        raise RecipeValidationError("too many link labels")
    for label in recipe.link_labels:
        if not label.strip() or len(label) > 100:
            raise RecipeValidationError("invalid link label")
    if len(recipe.regex_patterns) > 20:
        raise RecipeValidationError("too many regex patterns")
    for name, pattern in recipe.regex_patterns.items():
        if not name.strip() or len(name) > 100:
            raise RecipeValidationError("invalid regex field")
        _validate_pattern(pattern)


def apply_recipe(recipe: ParserRecipe, html: str, base_url: str) -> RecipeExtraction:
    validate_recipe(recipe)
    if len(html) > MAX_HTML_CHARS:
        raise RecipeValidationError("HTML exceeds recipe processing limit")
    host = (urlsplit(base_url).hostname or "").casefold().rstrip(".")
    if host != recipe.domain and not host.endswith(f".{recipe.domain}"):
        raise RecipeValidationError("recipe domain does not match page URL")
    soup = BeautifulSoup(html, "html.parser")
    if recipe.page_kind == PageKind.CATALOG:
        assert recipe.card_selector is not None
        cards = tuple(
            _extract_scope(recipe, node, base_url)
            for node in soup.select(recipe.card_selector)[:MAX_RESULTS_PER_FIELD]
        )
        return RecipeExtraction(cards=cards)
    detail = _extract_scope(recipe, soup, base_url)
    return RecipeExtraction(
        fields=detail.fields,
        links=detail.links,
        regex_matches=detail.regex_matches,
    )


def _extract_scope(recipe: ParserRecipe, scope: BeautifulSoup | Tag, base_url: str) -> RecipeCard:
    fields: dict[str, tuple[str, ...]] = {}
    for field_name, selector in recipe.field_selectors.items():
        values = tuple(
            dict.fromkeys(
                text
                for node in scope.select(selector)[:MAX_RESULTS_PER_FIELD]
                if (text := _normalized_text(node))
            )
        )
        if values:
            fields[field_name] = values
    links = _extract_labeled_links(scope, base_url, recipe.link_labels)
    scope_text = _normalized_text(scope)
    regex_matches = {
        name: matches
        for name, pattern in recipe.regex_patterns.items()
        if (matches := _regex_matches(pattern, scope_text))
    }
    return RecipeCard(fields=fields, links=links, regex_matches=regex_matches)


def _extract_labeled_links(
    scope: BeautifulSoup | Tag,
    base_url: str,
    labels: Iterable[str],
) -> tuple[RecipeLink, ...]:
    expected = {" ".join(label.casefold().split()) for label in labels}
    if not expected:
        return ()
    result: list[RecipeLink] = []
    seen: set[str] = set()
    for node in scope.select("a[href]"):
        text = _normalized_text(node)
        if text.casefold() not in expected:
            continue
        href = node.get("href")
        if not isinstance(href, str):
            continue
        url, _fragment = urldefrag(urljoin(base_url, href.strip()))
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or url in seen:
            continue
        seen.add(url)
        result.append(RecipeLink(text=text, url=url))
        if len(result) >= MAX_RESULTS_PER_FIELD:
            break
    return tuple(result)


def _validate_selector(selector: str) -> None:
    normalized = selector.strip().casefold()
    if not normalized or len(selector) > 200:
        raise RecipeValidationError("invalid CSS selector length")
    forbidden = ("script", "style", "iframe", "object", "embed", "javascript", "url(")
    if any(token in normalized for token in forbidden) or any(
        token in selector for token in (":", "@", "{", "}", ";")
    ):
        raise RecipeValidationError("unsafe CSS selector")
    try:
        soupsieve.compile(selector)
    except soupsieve.SelectorSyntaxError as exc:
        raise RecipeValidationError("invalid CSS selector") from exc


def _validate_pattern(pattern: str) -> None:
    if not pattern or len(pattern) > 200:
        raise RecipeValidationError("invalid regex length")
    if "(?" in pattern or re.search(r"\\[1-9]", pattern):
        raise RecipeValidationError("regex extensions and backreferences are forbidden")
    if re.search(r"\([^)]*[+*][^)]*\)\s*(?:[+*]|\{)", pattern):
        raise RecipeValidationError("nested regex quantifiers are forbidden")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise RecipeValidationError("invalid regex") from exc


def _regex_matches(pattern: str, text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(match.group(0) for match in re.finditer(pattern, text[:100_000]))
    )[:MAX_RESULTS_PER_FIELD]


def _normalized_text(node: BeautifulSoup | Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())
