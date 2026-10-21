import re
from dataclasses import dataclass
from datetime import date

from conference_scout.schemas.research import VerifiedConferenceResearch

_WORD_RE = re.compile(r"[a-zа-я0-9]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TopicRelevanceContext:
    name: str
    description: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KeywordRelevance:
    score: float
    matched_phrases: tuple[str, ...]
    explicitly_irrelevant: bool


def evaluate_keyword_relevance(
    title: str,
    summary: str | None,
    topics: tuple[TopicRelevanceContext, ...],
) -> KeywordRelevance:
    if not topics:
        return KeywordRelevance(1.0, (), False)

    haystack = _normalize(f"{title} {summary or ''}")
    candidate_tokens = set(_tokens(haystack))
    phrases = tuple(
        dict.fromkeys(
            phrase.strip()
            for topic in topics
            for phrase in (topic.name, *topic.keywords)
            if phrase.strip()
        )
    )
    matched = tuple(phrase for phrase in phrases if _normalize(phrase) in haystack)
    if matched:
        return KeywordRelevance(1.0, matched, False)

    topic_tokens = {
        token
        for topic in topics
        for value in (topic.name, topic.description, *topic.keywords)
        for token in _tokens(_normalize(value))
        if len(token) >= 3
    }
    overlap = candidate_tokens & topic_tokens
    if overlap:
        score = min(0.75, max(0.1, len(overlap) / max(1, len(topic_tokens))))
        return KeywordRelevance(score, (), False)
    return KeywordRelevance(0.0, (), bool(candidate_tokens))


def research_event_range(
    research: VerifiedConferenceResearch,
) -> tuple[date | None, date | None]:
    values = {claim.kind.casefold(): claim.value for claim in research.facts}
    return _parse_iso_date(values.get("start_date")), _parse_iso_date(values.get("end_date"))


def is_expired_research(research: VerifiedConferenceResearch, today: date) -> bool:
    start_date, end_date = research_event_range(research)
    if end_date is not None:
        return end_date < today
    return start_date is not None and start_date < today


def _normalize(value: str) -> str:
    return " ".join(_tokens(value.casefold().replace("ё", "е")))


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(value))


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
