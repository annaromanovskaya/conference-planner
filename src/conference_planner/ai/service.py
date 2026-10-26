import json
import re
from collections.abc import Callable
from difflib import SequenceMatcher
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from conference_planner.ai.prompts import (
    CATALOG_RESEARCH_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    RELEVANCE_SYSTEM_PROMPT,
    RESEARCH_EXTRACTION_SYSTEM_PROMPT,
    RESEARCH_RELEVANCE_SYSTEM_PROMPT,
    RESEARCH_VERIFICATION_SYSTEM_PROMPT,
    TOPIC_SYSTEM_PROMPT,
    catalog_research_user_prompt,
    extraction_user_prompt,
    research_extraction_user_prompt,
    research_verification_user_prompt,
)
from conference_planner.ai.provider import LLMProvider
from conference_planner.schemas.common import (
    DeadlineType,
    EvidenceSourceClass,
    VerificationStatus,
)
from conference_planner.schemas.conferences import ConferenceExtraction
from conference_planner.schemas.research import (
    ConferenceCatalogResearch,
    ConferenceResearch,
    DeadlineClaim,
    FactClaim,
    VerifiedConferenceResearch,
)
from conference_planner.schemas.topics import RelevanceAssessment, TopicNormalization

ValidatedModel = TypeVar("ValidatedModel", bound=BaseModel)


class AIResponseError(RuntimeError):
    pass


class AIService:
    MAX_RESEARCH_SOURCE_CHARS = 50_000

    def __init__(
        self,
        provider: LLMProvider,
        *,
        verifier_provider: LLMProvider | None = None,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.provider = provider
        self.verifier_provider = verifier_provider or provider
        self.max_attempts = max_attempts

    def build_extraction_prompt(self, source_url: str, text: str) -> str:
        return extraction_user_prompt(source_url, text)

    async def extract_conference(
        self,
        source_url: str,
        text: str,
        *,
        expected_name: str | None = None,
    ) -> ConferenceExtraction:
        prompt = self.build_extraction_prompt(source_url, text)
        if expected_name is not None:
            requested_name = json.dumps(expected_name, ensure_ascii=False)
            prompt = f"Requested conference title (data): {requested_name}\n{prompt}"
        result = await self._validated(
            EXTRACTION_SYSTEM_PROMPT,
            prompt,
            ConferenceExtraction,
            payload_transform=lambda payload: _normalize_conference_payload(
                payload, source_url, expected_name=expected_name
            ),
        )
        if str(result.source_url).rstrip("/") != source_url.rstrip("/"):
            raise AIResponseError("model changed the source URL")
        return result

    async def normalize_topic(self, natural_text: str) -> TopicNormalization:
        return await self._validated(TOPIC_SYSTEM_PROMPT, natural_text, TopicNormalization)

    async def rank_relevance(self, extraction: object, topic: object) -> RelevanceAssessment:
        payload = {
            "conference": self._public_mapping(extraction),
            "topic": self._public_mapping(topic),
        }
        return await self._validated(
            RELEVANCE_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False, default=str),
            RelevanceAssessment,
        )

    async def rank_research_relevance(
        self, research: object, topics: object
    ) -> RelevanceAssessment:
        payload = {
            "conference": self._public_mapping(research),
            "topics": self._public_mapping(topics),
        }
        return await self._validated(
            RESEARCH_RELEVANCE_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False, default=str),
            RelevanceAssessment,
        )

    async def extract_conference_research(
        self,
        source_url: str,
        text: str,
        expected_name: str,
    ) -> ConferenceResearch:
        prompt = research_extraction_user_prompt(
            source_url,
            text[: self.MAX_RESEARCH_SOURCE_CHARS],
            expected_name[:500],
        )
        result = await self._validated(
            RESEARCH_EXTRACTION_SYSTEM_PROMPT,
            prompt,
            ConferenceResearch,
            payload_transform=lambda payload: _normalize_research_payload(
                payload,
                source_url,
                expected_name,
            ),
        )
        if str(result.source_url).rstrip("/") != source_url.rstrip("/"):
            raise AIResponseError("model changed the research source URL")
        return result

    async def extract_conference_catalog(
        self,
        source_url: str,
        text: str,
    ) -> ConferenceCatalogResearch:
        result = await self._validated(
            CATALOG_RESEARCH_SYSTEM_PROMPT,
            catalog_research_user_prompt(
                source_url,
                text[: self.MAX_RESEARCH_SOURCE_CHARS],
            ),
            ConferenceCatalogResearch,
            payload_transform=lambda payload: _normalize_catalog_payload(payload, source_url),
        )
        if any(
            str(event.source_url).rstrip("/") != source_url.rstrip("/")
            for event in result.events
        ):
            raise AIResponseError("model changed a catalog research source URL")
        return result

    async def verify_conference_research(
        self,
        research: ConferenceResearch,
        *,
        evidence_documents: tuple[tuple[str, str], ...] = (),
    ) -> VerifiedConferenceResearch:
        verified = await self._validated(
            RESEARCH_VERIFICATION_SYSTEM_PROMPT,
            research_verification_user_prompt(
                research.model_dump_json(), evidence_documents
            ),
            VerifiedConferenceResearch,
            payload_transform=lambda payload: _normalize_research_payload(
                payload,
                str(research.source_url),
                research.name,
            ),
            provider=self.verifier_provider,
        )
        return _reconcile_verified_research(research, verified)

    async def _validated(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[ValidatedModel],
        *,
        payload_transform: Callable[[object], object] | None = None,
        provider: LLMProvider | None = None,
    ) -> ValidatedModel:
        last_error: Exception | None = None
        attempt_prompt = user_prompt
        for _ in range(self.max_attempts):
            raw = await (provider or self.provider).complete(
                system_prompt,
                attempt_prompt,
                response_schema=schema.model_json_schema(),
            )
            try:
                payload = json.loads(_extract_json(raw))
                if payload_transform is not None:
                    payload = payload_transform(payload)
                return schema.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                if isinstance(exc, ValidationError):
                    details = json.dumps(
                        exc.errors(include_url=False, include_input=False),
                        ensure_ascii=False,
                    )[:3_000]
                else:
                    details = "response was not valid JSON"
                attempt_prompt = (
                    f"{user_prompt}\n\nPrevious response was invalid: {details}\n"
                    "Return one JSON value that matches the supplied response schema exactly."
                )
        raise AIResponseError("LLM returned invalid structured output") from last_error

    @staticmethod
    def _public_mapping(value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return value
        if isinstance(value, (list, tuple)):
            return [AIService._public_mapping(item) for item in value]
        keys = ("name", "description", "keywords")
        return {key: getattr(value, key) for key in keys if hasattr(value, key)}


def _extract_json(raw: str) -> str:
    text = raw.strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return json.dumps(value, ensure_ascii=False)
    return text


def _normalize_conference_payload(
    payload: object, source_url: str, *, expected_name: str | None = None
) -> object:
    if isinstance(payload, list):
        payload = _select_conference_payload(payload, expected_name)
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    aliases = {
        "conference_name": "name",
        "website_url": "official_url",
    }
    missing = object()
    for alias, canonical in aliases.items():
        value = normalized.pop(alias, missing)
        if canonical not in normalized and value is not missing:
            normalized[canonical] = value
    normalized.pop("date", None)
    normalized.pop("organizer", None)
    normalized["source_url"] = source_url
    return normalized


def _select_conference_payload(payload: list[object], expected_name: str | None) -> object:
    candidates = [item for item in payload if isinstance(item, dict)]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates or expected_name is None:
        return payload

    expected = _comparable_name(expected_name)

    def score(candidate: dict) -> float:
        raw_name = candidate.get("name", candidate.get("conference_name", ""))
        name = _comparable_name(raw_name) if isinstance(raw_name, str) else ""
        return SequenceMatcher(None, expected, name).ratio()

    selected = max(candidates, key=score)
    return selected if score(selected) >= 0.35 else payload


def _comparable_name(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _select_research_payload(payload: object, expected_name: str) -> object:
    if not isinstance(payload, list):
        return payload
    return _select_conference_payload(payload, expected_name)


def _normalize_research_payload(
    payload: object,
    source_url: str,
    expected_name: str,
) -> object:
    payload = _select_research_payload(payload, expected_name)
    if not isinstance(payload, dict):
        return payload

    name = payload.get("name") or payload.get("conference") or payload.get("conference_name")
    if not isinstance(name, str) or not name.strip():
        name = expected_name
    aliases = payload.get("aliases", ())
    if not isinstance(aliases, (list, tuple)):
        aliases = ()

    normalized: dict[str, object] = {
        "name": name,
        "aliases": [item for item in aliases if isinstance(item, str) and item.strip()],
        "source_url": source_url,
        "source_class": _normalize_source_class(payload.get("source_class")),
        "facts": _normalize_fact_claims(
            payload.get("facts", payload.get("fact")), source_url
        ),
        "deadlines": _normalize_deadline_claims(
            payload.get("deadlines", payload.get("deadline")), source_url
        ),
    }
    notes = payload.get("verifier_notes")
    if isinstance(notes, (list, tuple)):
        normalized["verifier_notes"] = [
            item for item in notes if isinstance(item, str) and item.strip()
        ]
    return normalized


def _normalize_catalog_payload(payload: object, source_url: str) -> object:
    raw_events: object
    if isinstance(payload, list):
        raw_events = payload
    elif isinstance(payload, dict):
        raw_events = payload.get("events", payload.get("conferences", ()))
    else:
        return payload
    if not isinstance(raw_events, (list, tuple)):
        return {"events": []}

    events: list[object] = []
    seen_names: set[str] = set()
    for raw_event in raw_events[:50]:
        if not isinstance(raw_event, dict):
            continue
        raw_name = (
            raw_event.get("name")
            or raw_event.get("conference")
            or raw_event.get("conference_name")
        )
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        comparable_name = _comparable_name(raw_name)
        if not comparable_name or comparable_name in seen_names:
            continue
        normalized = _normalize_research_payload(raw_event, source_url, raw_name.strip())
        if not isinstance(normalized, dict):
            continue
        seen_names.add(comparable_name)
        events.append(normalized)
    return {"events": events}


def _normalize_fact_claims(value: object, source_url: str) -> list[dict[str, object]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    claims: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        fact_value = item.get("value") or item.get("fact")
        excerpt = item.get("excerpt") or item.get("evidence")
        kind = item.get("kind") or "description"
        if not all(isinstance(part, str) and part.strip() for part in (fact_value, excerpt, kind)):
            continue
        claims.append(
            {
                "kind": kind,
                "value": fact_value,
                "source_url": source_url,
                "source_class": _normalize_source_class(item.get("source_class")),
                "excerpt": excerpt,
                "verification_status": _normalize_verification_status(
                    item.get("verification_status")
                ),
            }
        )
    return claims


def _normalize_deadline_claims(value: object, source_url: str) -> list[dict[str, object]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    claims: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        excerpt = item.get("excerpt") or item.get("evidence")
        if not isinstance(excerpt, str) or not excerpt.strip():
            continue
        deadline_type = item.get("deadline_type", item.get("type"))
        if isinstance(deadline_type, str) and deadline_type.casefold() in {
            "event_date",
            "start_date",
            "end_date",
            "conference_date",
        }:
            continue
        if deadline_type not in {member.value for member in DeadlineType}:
            deadline_type = DeadlineType.OTHER.value
        exact_date = item.get("exact_date", item.get("date"))
        approximate_year = item.get("approximate_year", item.get("year"))
        approximate_month = item.get("approximate_month", item.get("month"))
        if (exact_date is None) == (approximate_year is None):
            continue
        claims.append(
            {
                "deadline_type": deadline_type,
                "exact_date": exact_date,
                "approximate_year": approximate_year,
                "approximate_month": approximate_month,
                "tentative": item.get("tentative", False),
                "source_url": source_url,
                "source_class": _normalize_source_class(item.get("source_class")),
                "excerpt": excerpt,
                "verification_status": _normalize_verification_status(
                    item.get("verification_status")
                ),
            }
        )
    return claims


def _normalize_source_class(value: object) -> str:
    valid = {member.value for member in EvidenceSourceClass}
    if isinstance(value, str) and value in valid:
        return value
    return EvidenceSourceClass.SECONDARY.value


def _normalize_verification_status(value: object) -> str:
    valid = {member.value for member in VerificationStatus}
    if isinstance(value, str) and value in valid:
        return value
    return VerificationStatus.UNVERIFIABLE.value


def _fact_key(claim: FactClaim) -> tuple[str, str, str, str]:
    return claim.kind, claim.value, str(claim.source_url), claim.excerpt


def _deadline_key(claim: DeadlineClaim) -> tuple[object, ...]:
    return (
        claim.deadline_type,
        claim.exact_date,
        claim.approximate_year,
        claim.approximate_month,
        str(claim.source_url),
        claim.excerpt,
    )


def _reconcile_verified_research(
    original: ConferenceResearch,
    reviewed: VerifiedConferenceResearch,
) -> VerifiedConferenceResearch:
    original_facts = {_fact_key(item): item for item in original.facts}
    original_deadlines = {_deadline_key(item): item for item in original.deadlines}

    facts: list[FactClaim] = []
    seen_fact_keys: set[tuple[str, str, str, str]] = set()
    for claim in reviewed.facts:
        key = _fact_key(claim)
        seen_fact_keys.add(key)
        status = (
            claim.verification_status if key in original_facts else VerificationStatus.UNVERIFIABLE
        )
        facts.append(claim.model_copy(update={"verification_status": status}))
    for key, claim in original_facts.items():
        if key not in seen_fact_keys:
            facts.append(
                claim.model_copy(update={"verification_status": VerificationStatus.UNVERIFIABLE})
            )

    deadlines: list[DeadlineClaim] = []
    seen_deadline_keys: set[tuple[object, ...]] = set()
    for claim in reviewed.deadlines:
        key = _deadline_key(claim)
        seen_deadline_keys.add(key)
        if key not in original_deadlines:
            status = VerificationStatus.UNVERIFIABLE
        elif claim.tentative:
            status = VerificationStatus.TENTATIVE
        else:
            status = claim.verification_status
        deadlines.append(claim.model_copy(update={"verification_status": status}))
    for key, claim in original_deadlines.items():
        if key not in seen_deadline_keys:
            deadlines.append(
                claim.model_copy(update={"verification_status": VerificationStatus.UNVERIFIABLE})
            )

    return VerifiedConferenceResearch(
        name=original.name,
        aliases=original.aliases,
        source_url=original.source_url,
        source_class=original.source_class,
        facts=tuple(facts),
        deadlines=tuple(deadlines),
        verifier_notes=reviewed.verifier_notes,
    )
