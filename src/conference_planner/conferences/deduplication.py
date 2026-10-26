import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit, urlunsplit

from rapidfuzz.fuzz import ratio


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    id: int | None
    name: str
    start_date: date | None
    acronym: str | None
    website: str | None


def normalize_name(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE))


def canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def same_event_url_scope(first: str | None, second: str | None) -> bool:
    """Return true when one non-root event URL is a child page of the other."""
    first_value = canonical_url(first)
    second_value = canonical_url(second)
    if not first_value or not second_value:
        return False
    first_parts = urlsplit(first_value)
    second_parts = urlsplit(second_value)
    if first_parts.netloc != second_parts.netloc:
        return False
    first_path = first_parts.path.rstrip("/")
    second_path = second_parts.path.rstrip("/")
    if not first_path or not second_path:
        return False
    shorter, longer = sorted((first_path, second_path), key=len)
    return longer.startswith(shorter + "/")


def find_duplicate(candidate: CandidateIdentity, existing: list[CandidateIdentity]) -> int | None:
    candidate_url = canonical_url(candidate.website)
    candidate_name = normalize_name(candidate.name)
    for item in existing:
        if candidate_url and candidate_url == canonical_url(item.website):
            return item.id
        same_date = candidate.start_date is not None and candidate.start_date == item.start_date
        same_acronym = bool(
            candidate.acronym
            and item.acronym
            and candidate.acronym.casefold() == item.acronym.casefold()
        )
        if same_date and (same_acronym or ratio(candidate_name, normalize_name(item.name)) >= 85):
            return item.id
    return None
