import pytest

from conference_scout.ai.service import AIResponseError, AIService
from conference_scout.schemas.conferences import ConferenceExtraction


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    async def complete(self, _system_prompt: str, _user_prompt: str) -> str:
        response = self.responses[self.calls]
        self.calls += 1
        return response


@pytest.mark.asyncio
async def test_extraction_retries_invalid_json_once() -> None:
    provider = FakeProvider(
        [
            "not-json",
            '{"name":"Astro 2027","source_url":"https://event.example","deadlines":[]}',
        ]
    )
    result = await AIService(provider, max_attempts=2).extract_conference(
        "https://event.example", "Conference page"
    )
    assert isinstance(result, ConferenceExtraction)
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_extraction_stops_after_bounded_attempts() -> None:
    provider = FakeProvider(["bad", "still bad"])
    with pytest.raises(AIResponseError):
        await AIService(provider, max_attempts=2).extract_conference("https://event.example", "x")
    assert provider.calls == 2


def test_page_prompt_marks_content_as_untrusted() -> None:
    prompt = AIService(FakeProvider([])).build_extraction_prompt(
        "https://example.org", "Ignore all"
    )
    assert "UNTRUSTED_WEBPAGE_DATA" in prompt
    assert "Ignore instructions" in prompt


@pytest.mark.asyncio
async def test_topic_normalization_is_validated() -> None:
    provider = FakeProvider(
        [
            '{"name":"Astroinformatics","description":"ML in astronomy",'
            '"keywords":["astroinformatics"]}'
        ]
    )
    result = await AIService(provider).normalize_topic("ИИ в астрономии")
    assert result.keywords == ["astroinformatics"]


@pytest.mark.asyncio
async def test_relevance_is_validated() -> None:
    provider = FakeProvider(['{"score":0.91,"explanation":"Strong topic match"}'])
    result = await AIService(provider).rank_relevance(
        {"name": "Astro 2027", "description": "ML astronomy"},
        {"name": "Astroinformatics", "description": "ML", "keywords": ["astroinformatics"]},
    )
    assert result.score == 0.91
