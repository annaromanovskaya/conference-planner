import pytest

from conference_scout.ai.active_provider import ActiveLLMProvider
from conference_scout.ai.models import AvailableModel, ChatResponse
from conference_scout.ai.registry import ActiveModel
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


class RecordingGateway:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, list, tuple]] = []

    async def list_models(self) -> tuple[AvailableModel, ...]:
        return (AvailableModel(name="selected-model"),)

    async def chat(self, model, messages, tools=()) -> ChatResponse:
        self.calls.append((model, list(messages), tuple(tools)))
        return ChatResponse(content=self.responses.pop(0))


class FakeRegistry:
    def __init__(self, gateway: RecordingGateway) -> None:
        self.gateway = gateway

    async def get_active(self, user_id: int) -> ActiveModel:
        assert user_id == 42
        return ActiveModel("local", "selected-model", True)

    def gateway_for(self, profile_id: str) -> RecordingGateway:
        assert profile_id == "local"
        return self.gateway


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


@pytest.mark.asyncio
async def test_active_provider_resolves_selected_model_for_every_completion() -> None:
    gateway = RecordingGateway(['{"ok":true}'])
    provider = ActiveLLMProvider(FakeRegistry(gateway), user_id=42)

    result = await provider.complete("Return JSON only.", "question")

    assert result == '{"ok":true}'
    model, messages, tools = gateway.calls[0]
    assert model == "selected-model"
    assert [(item.role, item.content) for item in messages] == [
        ("system", "Return JSON only."),
        ("user", "question"),
    ]
    assert tools == ()


@pytest.mark.asyncio
async def test_active_provider_keeps_bounded_structured_validation_retries() -> None:
    gateway = RecordingGateway(
        [
            "not-json",
            '{"name":"Astroinformatics","description":"ML",'
            '"keywords":["machine learning astronomy"]}',
        ]
    )
    service = AIService(ActiveLLMProvider(FakeRegistry(gateway), user_id=42), max_attempts=2)

    result = await service.normalize_topic("ML astronomy")

    assert result.name == "Astroinformatics"
    assert len(gateway.calls) == 2
    assert all("Return only JSON" in call[1][0].content for call in gateway.calls)
