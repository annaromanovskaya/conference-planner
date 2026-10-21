import re
from datetime import date
from difflib import SequenceMatcher
from urllib.parse import unquote, urlsplit

from conference_scout.schemas.common import (
    DeadlineType,
    EvidenceSourceClass,
    VerificationStatus,
)
from conference_scout.schemas.research import (
    ConferenceCatalogResearch,
    ConferenceResearch,
    DeadlineClaim,
    FactClaim,
)
from conference_scout.web.parser import PageLink

_MONTHS = {
    "january": 1,
    "jan": 1,
    "январь": 1,
    "января": 1,
    "february": 2,
    "feb": 2,
    "февраль": 2,
    "февраля": 2,
    "march": 3,
    "mar": 3,
    "март": 3,
    "марта": 3,
    "april": 4,
    "apr": 4,
    "апрель": 4,
    "апреля": 4,
    "may": 5,
    "май": 5,
    "мая": 5,
    "june": 6,
    "jun": 6,
    "июнь": 6,
    "июня": 6,
    "july": 7,
    "jul": 7,
    "июль": 7,
    "июля": 7,
    "august": 8,
    "aug": 8,
    "август": 8,
    "августа": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "сентябрь": 9,
    "сентября": 9,
    "october": 10,
    "oct": 10,
    "октябрь": 10,
    "октября": 10,
    "november": 11,
    "nov": 11,
    "ноябрь": 11,
    "ноября": 11,
    "december": 12,
    "dec": 12,
    "декабрь": 12,
    "декабря": 12,
}
_MONTH_PATTERN = "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True))
_DAY_FIRST_RANGE = re.compile(
    rf"(?<!\d)(?P<start>\d{{1,2}})(?:st|nd|rd|th)?\s*[-–—]\s*"
    rf"(?P<end>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{_MONTH_PATTERN})\s+(?P<year>20\d{{2}})",
    re.IGNORECASE,
)
_MONTH_FIRST_RANGE = re.compile(
    rf"(?P<month>{_MONTH_PATTERN})\s+"
    rf"(?P<start>\d{{1,2}})(?:st|nd|rd|th)?\s*[-–—]\s*"
    rf"(?P<end>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(?P<year>20\d{{2}})",
    re.IGNORECASE,
)
_WORD_RANGE = re.compile(
    rf"(?:from|с)\s+(?P<start>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?:to|по)\s+(?P<end>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{_MONTH_PATTERN})\s+(?P<year>20\d{{2}})",
    re.IGNORECASE,
)
_DAY_FIRST = re.compile(
    rf"(?<!\d)(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+"
    rf"(?P<month>{_MONTH_PATTERN})\s+(?P<year>20\d{{2}})",
    re.IGNORECASE,
)
_MONTH_FIRST = re.compile(
    rf"(?P<month>{_MONTH_PATTERN})\s+"
    rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(?P<year>20\d{{2}})",
    re.IGNORECASE,
)
_MONTH_YEAR = re.compile(rf"(?P<month>{_MONTH_PATTERN})\s+(?P<year>20\d{{2}})", re.IGNORECASE)
_EVENT_MARKERS = (
    "starts",
    "begins",
    "takes place",
    "conference week",
    "состоится",
    "начинается",
    "пройдет",
    "проходить",
)
_DEADLINE_MARKERS = (
    "deadline",
    "early bird",
    "closes",
    "opens",
    "registration",
    "submission",
    "notification",
    "acceptance",
    "abstract",
    "poster",
    "payment",
    "дедлайн",
    "срок",
    "подач",
    "регистрац",
    "тезис",
    "аннотац",
    "уведом",
    "постер",
    "оплат",
)


def extract_temporal_research(
    source_url: str,
    text: str,
    expected_name: str,
) -> ConferenceResearch:
    """Extract explicitly printed dates without asking a language model."""
    if not _mentions_expected_event(text, expected_name):
        return ConferenceResearch(
            name=expected_name,
            source_url=source_url,
            source_class=EvidenceSourceClass.OFFICIAL_CONFERENCE,
        )
    facts: list[FactClaim] = []
    deadlines: list[DeadlineClaim] = []
    global_tentative = any(marker in _normalized(text) for marker in ("tentative", "предварител"))
    seen_facts: set[tuple[str, str]] = set()
    seen_deadlines: set[tuple[DeadlineType, date | None, int | None, int | None]] = set()
    event_range_found = False

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())[:1_000]
        if not line:
            continue
        normalized = _normalized(line)
        deadline_line = any(marker in normalized for marker in _DEADLINE_MARKERS)

        range_match = (
            _DAY_FIRST_RANGE.search(line)
            or _MONTH_FIRST_RANGE.search(line)
            or _WORD_RANGE.search(line)
        )
        if range_match is not None and not deadline_line and not event_range_found:
            month = _month_number(range_match.group("month"))
            year = int(range_match.group("year"))
            _append_fact(
                facts,
                seen_facts,
                source_url,
                "start_date",
                date(year, month, int(range_match.group("start"))),
                line,
            )
            _append_fact(
                facts,
                seen_facts,
                source_url,
                "end_date",
                date(year, month, int(range_match.group("end"))),
                line,
            )
            event_range_found = True
            continue

        exact_match = _DAY_FIRST.search(line) or _MONTH_FIRST.search(line)
        if deadline_line:
            if exact_match is not None:
                exact = date(
                    int(exact_match.group("year")),
                    _month_number(exact_match.group("month")),
                    int(exact_match.group("day")),
                )
                for deadline_type in _deadline_types(normalized):
                    identity = (deadline_type, exact, None, None)
                    if identity not in seen_deadlines:
                        seen_deadlines.add(identity)
                        deadlines.append(
                            DeadlineClaim(
                                deadline_type=deadline_type,
                                exact_date=exact,
                                tentative=global_tentative,
                                source_url=source_url,
                                source_class=EvidenceSourceClass.OFFICIAL_CONFERENCE,
                                excerpt=line,
                                verification_status=(
                                    VerificationStatus.TENTATIVE
                                    if global_tentative
                                    else VerificationStatus.OFFICIAL_CONFIRMED
                                ),
                            )
                        )
                continue
            month_match = _MONTH_YEAR.search(line)
            if month_match is not None:
                year = int(month_match.group("year"))
                month = _month_number(month_match.group("month"))
                for deadline_type in _deadline_types(normalized):
                    identity = (deadline_type, None, year, month)
                    if identity not in seen_deadlines:
                        seen_deadlines.add(identity)
                        deadlines.append(
                            DeadlineClaim(
                                deadline_type=deadline_type,
                                approximate_year=year,
                                approximate_month=month,
                                tentative=True,
                                source_url=source_url,
                                source_class=EvidenceSourceClass.OFFICIAL_CONFERENCE,
                                excerpt=line,
                                verification_status=VerificationStatus.TENTATIVE,
                            )
                        )
                continue

        if exact_match is not None and any(marker in normalized for marker in _EVENT_MARKERS):
            value = date(
                int(exact_match.group("year")),
                _month_number(exact_match.group("month")),
                int(exact_match.group("day")),
            )
            _append_fact(facts, seen_facts, source_url, "start_date", value, line)

    return ConferenceResearch(
        name=expected_name,
        source_url=source_url,
        source_class=EvidenceSourceClass.OFFICIAL_CONFERENCE,
        facts=tuple(facts),
        deadlines=tuple(deadlines),
    )


def merge_temporal_research(
    primary: ConferenceResearch,
    deterministic: ConferenceResearch,
) -> ConferenceResearch:
    if primary.source_class == EvidenceSourceClass.SECONDARY:
        facts = [_as_secondary(claim) for claim in deterministic.facts]
        fact_keys = {(claim.kind, claim.value) for claim in facts}
        facts.extend(
            claim for claim in primary.facts if (claim.kind, claim.value) not in fact_keys
        )
        deadlines = [_as_secondary(claim) for claim in deterministic.deadlines]
        deadline_keys = {_deadline_identity(claim) for claim in deadlines}
        specific_dates = {
            _deadline_temporal_identity(claim)
            for claim in deadlines
            if claim.deadline_type != DeadlineType.OTHER
        }
        deadlines.extend(
            claim
            for claim in primary.deadlines
            if _deadline_identity(claim) not in deadline_keys
            and not (
                claim.deadline_type == DeadlineType.OTHER
                and _deadline_temporal_identity(claim) in specific_dates
            )
        )
        return primary.model_copy(update={"facts": tuple(facts), "deadlines": tuple(deadlines)})

    facts = list(deterministic.facts)
    seen_facts = {(claim.kind, claim.value) for claim in facts}
    facts.extend(claim for claim in primary.facts if (claim.kind, claim.value) not in seen_facts)

    deadlines = list(deterministic.deadlines)
    seen_deadlines = {_deadline_identity(claim) for claim in deadlines}
    deadlines.extend(
        claim for claim in primary.deadlines if _deadline_identity(claim) not in seen_deadlines
    )
    return primary.model_copy(update={"facts": tuple(facts), "deadlines": tuple(deadlines)})


def enrich_catalog_events(
    catalog: ConferenceCatalogResearch,
    source_url: str,
    text: str,
    links: tuple[PageLink, ...],
) -> ConferenceCatalogResearch:
    """Enrich catalog events only from the page block belonging to each event."""
    lines = tuple(line.strip() for line in text.splitlines() if line.strip())
    if not lines or not catalog.events:
        return catalog

    anchors = tuple(_event_anchor(event.name, lines) for event in catalog.events)
    enriched: list[ConferenceResearch] = []
    for position, event in enumerate(catalog.events):
        anchor = anchors[position]
        if anchor is None:
            enriched.append(event)
            continue
        following = [value for value in anchors if value is not None and value > anchor]
        end = min(following) if following else len(lines)
        block_lines = lines[anchor:end]
        block = "\n".join(block_lines)
        deterministic = extract_temporal_research(source_url, block, event.name)
        current = merge_temporal_research(event, deterministic)
        current = _add_catalog_location(current, source_url, block_lines)
        current = _add_catalog_official_url(current, source_url, block, links)
        enriched.append(current)
    return catalog.model_copy(update={"events": tuple(enriched)})


def extract_catalog_link_events(
    source_url: str,
    expected_name: str,
    links: tuple[PageLink, ...],
    *,
    min_year: int | None = None,
) -> ConferenceCatalogResearch:
    year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", f"{expected_name} {source_url}")
    explicit_year = year_match.group(1) if year_match is not None else None
    min_year = min_year or date.today().year
    source = source_url.rstrip("/")
    source_path = urlsplit(source_url).path.rstrip("/")
    source_parent = source_path.removesuffix(".html") + "/"
    events: list[ConferenceResearch] = []
    seen_urls: set[str] = set()
    for link in links:
        target = link.url.rstrip("/")
        parsed_target = urlsplit(link.url)
        target_path = parsed_target.path
        label = " ".join(link.text.split())
        if (
            not label
            or target == source
            or target in seen_urls
            or _blocked_catalog_target(parsed_target.hostname or "", target_path)
        ):
            continue
        if explicit_year is not None:
            if explicit_year not in target_path or not target_path.startswith(source_parent):
                continue
        else:
            years = {
                int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", f"{label} {link.url}")
            }
            if not any(min_year <= year <= min_year + 5 for year in years):
                continue
        normalized_label = _normalized(label)
        generic_labels = {
            f"conferences {explicit_year}",
            f"meetings {explicit_year}",
            f"конференции {explicit_year}",
        }
        if explicit_year is not None and normalized_label in generic_labels:
            continue
        seen_urls.add(target)
        title = _catalog_link_title(label, link.url)
        if not title:
            continue
        events.append(
            ConferenceResearch(
                name=title[:500],
                source_url=link.url,
                source_class=EvidenceSourceClass.OFFICIAL_ORGANIZER,
            )
        )
    return ConferenceCatalogResearch(events=tuple(events[:50]))


def _append_fact(
    facts: list[FactClaim],
    seen: set[tuple[str, str]],
    source_url: str,
    kind: str,
    value: date,
    excerpt: str,
) -> None:
    identity = (kind, value.isoformat())
    if identity in seen:
        return
    seen.add(identity)
    facts.append(
        FactClaim(
            kind=kind,
            value=value.isoformat(),
            source_url=source_url,
            source_class=EvidenceSourceClass.OFFICIAL_CONFERENCE,
            excerpt=excerpt,
            verification_status=VerificationStatus.OFFICIAL_CONFIRMED,
        )
    )


def _deadline_type(line: str) -> DeadlineType:
    if "registration" in line and any(marker in line for marker in ("opens", "opening")):
        return DeadlineType.REGISTRATION_OPEN
    if any(marker in line for marker in ("oral", "устн")) and any(
        marker in line for marker in ("abstract", "submission", "тезис", "подач")
    ):
        return DeadlineType.ORAL_ABSTRACT_SUBMISSION
    if any(marker in line for marker in ("notification", "acceptance", "уведом")):
        return DeadlineType.ACCEPTANCE_NOTIFICATION
    if any(marker in line for marker in ("early bird", "ранн")):
        return DeadlineType.EARLY_REGISTRATION
    if any(marker in line for marker in ("registration", "регистрац")):
        return DeadlineType.REGISTRATION
    if any(marker in line for marker in ("poster", "постер")):
        return DeadlineType.POSTER_SUBMISSION
    if any(marker in line for marker in ("payment", "оплат")):
        return DeadlineType.PAYMENT
    if any(marker in line for marker in ("proceedings", "publication", "публикац")):
        return DeadlineType.OTHER
    if any(
        marker in line
        for marker in ("abstract", "submission", "тезис", "подач", "аннотац")
    ):
        return DeadlineType.ABSTRACT_SUBMISSION
    return DeadlineType.OTHER


def _deadline_types(line: str) -> tuple[DeadlineType, ...]:
    primary = _deadline_type(line)
    has_registration = any(marker in line for marker in ("registration", "регистрац"))
    has_abstract = any(
        marker in line for marker in ("abstract", "тезис", "аннотац")
    )
    is_opening = any(marker in line for marker in ("opens", "opening", "открыт"))
    if has_registration and has_abstract and not is_opening:
        return DeadlineType.REGISTRATION, DeadlineType.ABSTRACT_SUBMISSION
    return (primary,)


def _month_number(value: str) -> int:
    return _MONTHS[value.casefold().replace("ё", "е")]


def _deadline_identity(
    claim: DeadlineClaim,
) -> tuple[DeadlineType, date | None, int | None, int | None]:
    return (
        claim.deadline_type,
        claim.exact_date,
        claim.approximate_year,
        claim.approximate_month,
    )


def _deadline_temporal_identity(
    claim: DeadlineClaim,
) -> tuple[date | None, int | None, int | None]:
    return claim.exact_date, claim.approximate_year, claim.approximate_month


def _event_anchor(name: str, lines: tuple[str, ...]) -> int | None:
    expected = _normalized(name)
    if not expected:
        return None
    ranked = [
        (SequenceMatcher(None, expected, _normalized(line)).ratio(), index)
        for index, line in enumerate(lines)
    ]
    score, index = max(ranked, default=(0.0, 0))
    return index if score >= 0.5 else None


def _claim_status(source_class: EvidenceSourceClass) -> VerificationStatus:
    return {
        EvidenceSourceClass.OFFICIAL_CONFERENCE: VerificationStatus.OFFICIAL_CONFIRMED,
        EvidenceSourceClass.OFFICIAL_ORGANIZER: VerificationStatus.ORGANIZER_CONFIRMED,
        EvidenceSourceClass.SECONDARY: VerificationStatus.SECONDARY_UNVERIFIED,
    }[source_class]


def _add_catalog_location(
    research: ConferenceResearch,
    source_url: str,
    lines: list[str] | tuple[str, ...],
) -> ConferenceResearch:
    if any(claim.kind.casefold() == "location" for claim in research.facts):
        return research
    pattern = re.compile(
        rf"\((?P<location>[^()\n]{{2,100}}?),\s*(?={_MONTH_PATTERN}\s+\d{{1,2}})",
        re.IGNORECASE,
    )
    relevant_lines = lines[:5]
    for line in relevant_lines:
        match = pattern.search(line)
        if match is None:
            continue
        location = " ".join(match.group("location").replace(",", ", ").split())
        claim = FactClaim(
            kind="location",
            value=location,
            source_url=source_url,
            source_class=research.source_class,
            excerpt=line[:1_000],
            verification_status=_claim_status(research.source_class),
        )
        return research.model_copy(update={"facts": (*research.facts, claim)})
    joined = " ".join(relevant_lines)
    prose_patterns = (
        re.compile(
            r"(?:в\s+г\.?\s*)(?P<location>[^.;]{2,120}?)(?=,\s*с\s+\d{1,2}\s+по)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?:held|takes place)\s+in\s+(?P<location>[^.;]{{2,120}}?)"
            rf"(?=,\s*(?:on\s+)?(?:{_MONTH_PATTERN}|from\s+\d))",
            re.IGNORECASE,
        ),
    )
    for prose_pattern in prose_patterns:
        match = prose_pattern.search(joined)
        if match is None:
            continue
        location = " ".join(match.group("location").replace(",", ", ").split())
        claim = FactClaim(
            kind="location",
            value=location,
            source_url=source_url,
            source_class=research.source_class,
            excerpt=joined[:1_000],
            verification_status=_claim_status(research.source_class),
        )
        return research.model_copy(update={"facts": (*research.facts, claim)})
    return research


def _add_catalog_official_url(
    research: ConferenceResearch,
    source_url: str,
    block: str,
    links: tuple[PageLink, ...],
) -> ConferenceResearch:
    if any(claim.kind.casefold() == "official_url" for claim in research.facts):
        return research
    normalized_block = _normalized(block)
    source_host = (urlsplit(source_url).hostname or "").casefold()
    candidates: list[tuple[int, PageLink]] = []
    for link in links:
        label = _normalized(link.text)
        if (
            not label
            or label not in normalized_block
            or link.url.rstrip("/") == source_url.rstrip("/")
        ):
            continue
        score = 0
        if link.text.casefold().startswith(("http://", "https://")):
            score += 5
        if any(marker in label for marker in ("official", "website", "conference", "event")):
            score += 4
        if SequenceMatcher(None, _normalized(research.name), label).ratio() >= 0.5:
            score += 4
        if (urlsplit(link.url).hostname or "").casefold() != source_host:
            score += 1
        candidates.append((score, link))
    if not candidates:
        return research
    score, selected = max(candidates, key=lambda item: item[0])
    if score < 4:
        return research
    claim = FactClaim(
        kind="official_url",
        value=selected.url,
        source_url=source_url,
        source_class=research.source_class,
        excerpt=selected.text[:1_000],
        verification_status=_claim_status(research.source_class),
    )
    return ConferenceResearch.model_validate(
        {
            **research.model_dump(mode="python"),
            "source_url": selected.url,
            "facts": (*research.facts, claim),
        }
    )


def _as_secondary(claim: FactClaim | DeadlineClaim) -> FactClaim | DeadlineClaim:
    status = (
        VerificationStatus.TENTATIVE
        if claim.verification_status == VerificationStatus.TENTATIVE
        else VerificationStatus.SECONDARY_UNVERIFIED
    )
    return claim.model_copy(
        update={
            "source_class": EvidenceSourceClass.SECONDARY,
            "verification_status": status,
        }
    )


def _normalized(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _mentions_expected_event(text: str, expected_name: str) -> bool:
    haystack = _normalized(text)
    compact_haystack = re.sub(r"[^a-zа-я0-9]+", "", haystack)
    generic = {
        "conference",
        "meeting",
        "meetings",
        "general",
        "assembly",
        "international",
        "конференция",
        "конференции",
        "международная",
    }
    tokens = [
        token
        for token in re.findall(r"[a-zа-я0-9]+", _normalized(expected_name))
        if len(token) >= 4 and token not in generic and not token.isdigit()
    ]
    if any(token in haystack for token in tokens):
        return True
    compact_expected = re.sub(r"[^a-zа-я0-9]+", "", _normalized(expected_name))
    return len(compact_expected) >= 6 and compact_expected in compact_haystack


def _blocked_catalog_target(hostname: str, path: str) -> bool:
    blocked_hosts = (
        "forms.yandex.",
        "youtube.com",
        "youtu.be",
        "rutube.ru",
        "zoom.us",
    )
    blocked_suffixes = (".pdf", ".doc", ".docx", ".zip", ".jpg", ".png")
    host = hostname.casefold()
    clean_path = path.casefold().rstrip("/")
    return any(marker in host for marker in blocked_hosts) or clean_path.endswith(blocked_suffixes)


def _catalog_link_title(label: str, url: str) -> str:
    normalized_label = label.casefold().strip()
    if not normalized_label.startswith(("http://", "https://", "www.")):
        return label
    parsed = urlsplit(url)
    slug = unquote(parsed.path).rstrip("/").rsplit("/", maxsplit=1)[-1]
    slug = re.sub(r"\.(?:html?|php|aspx?)$", "", slug, flags=re.IGNORECASE)
    title = " ".join(part for part in re.split(r"[-_]+", slug) if part)
    return title or (parsed.hostname or "")
