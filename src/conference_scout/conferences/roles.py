import inspect
from typing import Protocol

from conference_scout.conferences.evidence import EvidenceDocument
from conference_scout.schemas.research import ConferenceResearch, VerifiedConferenceResearch


class ResearchAnalyzer(Protocol):
    async def extract_conference_research(
        self, source_url: str, text: str, expected_name: str
    ) -> ConferenceResearch: ...


class VerificationAnalyzer(Protocol):
    async def verify_conference_research(
        self,
        research: ConferenceResearch,
        *,
        evidence_documents: tuple[tuple[str, str], ...] = (),
    ) -> VerifiedConferenceResearch: ...


class ResearchRole:
    def __init__(self, analyzer: ResearchAnalyzer) -> None:
        self.analyzer = analyzer

    async def extract(
        self,
        documents: tuple[EvidenceDocument, ...],
        expected_name: str,
    ) -> ConferenceResearch:
        if not documents:
            raise ValueError("research requires at least one evidence document")
        text = "\n\n".join(
            f"DOCUMENT_URL: {item.url}\nDOCUMENT_TEXT:\n{item.text}\nEND_DOCUMENT"
            for item in documents
        )
        return await self.analyzer.extract_conference_research(
            documents[0].url, text, expected_name
        )


class VerifierRole:
    def __init__(self, analyzer: VerificationAnalyzer) -> None:
        self.analyzer = analyzer

    async def verify(
        self,
        research: ConferenceResearch,
        documents: tuple[EvidenceDocument, ...],
    ) -> VerifiedConferenceResearch:
        method = self.analyzer.verify_conference_research
        if "evidence_documents" in inspect.signature(method).parameters:
            return await method(
                research,
                evidence_documents=tuple((item.url, item.text) for item in documents),
            )
        return await method(research)
