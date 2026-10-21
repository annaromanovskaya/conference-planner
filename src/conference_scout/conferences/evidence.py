import re
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

from conference_scout.schemas.common import (
    ArtifactValidationState,
    EvidenceSourceClass,
    VerificationStatus,
)
from conference_scout.schemas.research import (
    ConferenceResearch,
    DeadlineClaim,
    EvidenceValidationItem,
    EvidenceValidationReport,
    FactClaim,
    VerifiedConferenceResearch,
)


@dataclass(frozen=True, slots=True)
class EvidenceDocument:
    url: str
    text: str
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationContext:
    excerpt_found: bool
    deterministic_date: bool
    source_conflict: bool = False
    snippet_only: bool = False
    changes_saved_deadline: bool = False
    first_important_model_fact: bool = False


def verification_required(
    claim: FactClaim | DeadlineClaim,
    context: VerificationContext,
) -> bool:
    return any(
        (
            claim.source_class == EvidenceSourceClass.SECONDARY,
            claim.verification_status
            in {
                VerificationStatus.CONFLICT,
                VerificationStatus.UNVERIFIABLE,
                VerificationStatus.SECONDARY_UNVERIFIED,
            },
            isinstance(claim, DeadlineClaim) and claim.exact_date is None,
            isinstance(claim, DeadlineClaim) and claim.tentative,
            not context.excerpt_found,
            not context.deterministic_date,
            context.source_conflict,
            context.snippet_only,
            context.changes_saved_deadline,
            context.first_important_model_fact,
        )
    )


class EvidenceValidator:
    _MONTH_NAMES: ClassVar[dict[int, tuple[str, ...]]] = {
        1: ("january", "jan", "январ"),
        2: ("february", "feb", "феврал"),
        3: ("march", "mar", "март"),
        4: ("april", "apr", "апрел"),
        5: ("may", "май", "мая"),
        6: ("june", "jun", "июн"),
        7: ("july", "jul", "июл"),
        8: ("august", "aug", "август"),
        9: ("september", "sep", "сентябр"),
        10: ("october", "oct", "октябр"),
        11: ("november", "nov", "ноябр"),
        12: ("december", "dec", "декабр"),
    }
    _DEADLINE_MARKERS = (
        "deadline",
        "closes",
        "close",
        "due",
        "submission",
        "registration",
        "payment",
        "abstract",
        "poster",
        "notification",
        "срок",
        "подач",
        "регистрац",
        "оплат",
        "тезис",
    )
    _EVENT_MARKERS = ("conference starts", "meeting starts", "takes place", "состоится")

    def validate(
        self,
        research: ConferenceResearch,
        documents: tuple[EvidenceDocument, ...],
    ) -> EvidenceValidationReport:
        accepted_facts: list[FactClaim] = []
        accepted_deadlines: list[DeadlineClaim] = []
        results: list[EvidenceValidationItem] = []
        for index, claim in enumerate(research.facts):
            state, reason = self._validate_claim(claim, documents)
            results.append(EvidenceValidationItem(
                claim_type="fact", claim_index=index, state=state, reason=reason
            ))
            if state in {ArtifactValidationState.ACCEPTED, ArtifactValidationState.TENTATIVE}:
                accepted_facts.append(claim)
        for index, claim in enumerate(research.deadlines):
            state, reason = self._validate_claim(claim, documents)
            if state not in {ArtifactValidationState.REJECTED, ArtifactValidationState.CONFLICT}:
                excerpt = _normalize(claim.excerpt)
                if any(marker in excerpt for marker in self._EVENT_MARKERS) and not any(
                    marker in excerpt for marker in self._DEADLINE_MARKERS
                ):
                    state, reason = (
                        ArtifactValidationState.REJECTED,
                        "EVENT_DATE_IS_NOT_DEADLINE",
                    )
            results.append(EvidenceValidationItem(
                claim_type="deadline", claim_index=index, state=state, reason=reason
            ))
            if state in {ArtifactValidationState.ACCEPTED, ArtifactValidationState.TENTATIVE}:
                accepted_deadlines.append(claim)
        payload = research.model_dump(mode="python")
        payload["facts"] = tuple(accepted_facts)
        payload["deadlines"] = tuple(accepted_deadlines)
        payload.setdefault("verifier_notes", ())
        return EvidenceValidationReport(
            validated=VerifiedConferenceResearch.model_validate(payload),
            results=tuple(results),
        )

    def context_for(
        self,
        claim: FactClaim | DeadlineClaim,
        documents: tuple[EvidenceDocument, ...],
        **overrides: bool,
    ) -> VerificationContext:
        document = self._cited_document(claim, documents)
        excerpt_found = bool(
            document and _normalize(claim.excerpt) in _normalize(document.text)
        )
        return VerificationContext(
            excerpt_found=excerpt_found,
            deterministic_date=self._date_supported(claim),
            **overrides,
        )

    def _validate_claim(
        self,
        claim: FactClaim | DeadlineClaim,
        documents: tuple[EvidenceDocument, ...],
    ) -> tuple[ArtifactValidationState, str]:
        if claim.verification_status == VerificationStatus.CONFLICT:
            return ArtifactValidationState.CONFLICT, "SOURCE_CONFLICT"
        document = self._cited_document(claim, documents)
        if document is None:
            return ArtifactValidationState.REJECTED, "SOURCE_NOT_IN_RUN"
        if _normalize(claim.excerpt) not in _normalize(document.text):
            return ArtifactValidationState.REJECTED, "EXCERPT_NOT_FOUND"
        if not self._date_supported(claim):
            return ArtifactValidationState.REJECTED, "DATE_NOT_SUPPORTED"
        if (
            claim.verification_status == VerificationStatus.TENTATIVE
            or (
                isinstance(claim, DeadlineClaim)
                and (claim.tentative or claim.exact_date is None)
            )
        ):
            return ArtifactValidationState.TENTATIVE, "TENTATIVE_EVIDENCE"
        return ArtifactValidationState.ACCEPTED, "EVIDENCE_CONFIRMED"

    @staticmethod
    def _cited_document(
        claim: FactClaim | DeadlineClaim,
        documents: tuple[EvidenceDocument, ...],
    ) -> EvidenceDocument | None:
        source = str(claim.source_url).rstrip("/")
        return next((item for item in documents if item.url.rstrip("/") == source), None)

    def _date_supported(self, claim: FactClaim | DeadlineClaim) -> bool:
        if isinstance(claim, FactClaim):
            if claim.kind.casefold() not in {"start_date", "end_date", "event_date"}:
                return True
            try:
                value = date.fromisoformat(claim.value)
            except ValueError:
                return False
            return self._date_in_excerpt(value, claim.excerpt)
        if claim.exact_date is not None:
            return self._date_in_excerpt(claim.exact_date, claim.excerpt)
        excerpt = _normalize(claim.excerpt)
        if str(claim.approximate_year) not in excerpt:
            return False
        if claim.approximate_month is None:
            return True
        return any(
            marker in excerpt for marker in self._MONTH_NAMES[claim.approximate_month]
        )

    def _date_in_excerpt(self, value: date, excerpt: str) -> bool:
        normalized = _normalize(excerpt)
        if value.isoformat() in normalized:
            return True
        if str(value.year) not in normalized:
            return False
        if not re.search(rf"(?<!\d)0?{value.day}(?:st|nd|rd|th)?(?!\d)", normalized):
            return False
        return any(marker in normalized for marker in self._MONTH_NAMES[value.month])


def _normalize(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())
