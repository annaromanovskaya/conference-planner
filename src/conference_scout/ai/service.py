import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from conference_scout.ai.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    RELEVANCE_SYSTEM_PROMPT,
    TOPIC_SYSTEM_PROMPT,
    extraction_user_prompt,
)
from conference_scout.ai.provider import LLMProvider
from conference_scout.schemas.conferences import ConferenceExtraction
from conference_scout.schemas.topics import RelevanceAssessment, TopicNormalization

ValidatedModel = TypeVar("ValidatedModel", bound=BaseModel)


class AIResponseError(RuntimeError):
    pass


class AIService:
    def __init__(self, provider: LLMProvider, *, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.provider = provider
        self.max_attempts = max_attempts

    def build_extraction_prompt(self, source_url: str, text: str) -> str:
        return extraction_user_prompt(source_url, text)

    async def extract_conference(self, source_url: str, text: str) -> ConferenceExtraction:
        prompt = self.build_extraction_prompt(source_url, text)
        result = await self._validated(EXTRACTION_SYSTEM_PROMPT, prompt, ConferenceExtraction)
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

    async def _validated(
        self, system_prompt: str, user_prompt: str, schema: type[ValidatedModel]
    ) -> ValidatedModel:
        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            raw = await self.provider.complete(system_prompt, user_prompt)
            try:
                return schema.model_validate_json(raw)
            except ValidationError as exc:
                last_error = exc
        raise AIResponseError("LLM returned invalid structured output") from last_error

    @staticmethod
    def _public_mapping(value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return value
        keys = ("name", "description", "keywords")
        return {key: getattr(value, key) for key in keys if hasattr(value, key)}
