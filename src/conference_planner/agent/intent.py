from __future__ import annotations
"""Semantic routing for expensive durable conference operations."""
import asyncio
import re
from conference_planner.ai.models import ChatMessage

async def _wants_bulk_candidate_review(text: str, runtime: AgentRuntime, user_id: int) -> bool:
    """Cheap direct dispatch for clear requests; semantic tool intent for paraphrases.

    The model selects an intent only, never creates or executes an action. Bulk
    operations are always validated and persisted by the deterministic handler.
    """
    normalized = text.casefold()
    if not re.search(r"кандидат|candidate", normalized):
        return False
    if re.search(
        r"(?:проверь|проверить|исследуй|исследовать|перепроверь|обработай|проверка)"
        r".{0,55}(?:все|всех|оставшиеся|ожидающие|эти|каждого).{0,30}кандидат"
        r"|(?:все|всех|оставшиеся|ожидающие).{0,35}кандидат.{0,35}"
        r"(?:проверь|проверить|исследуй|обработай)"
        r"|(?:review|verify|check).{0,45}(?:all|every|pending).{0,30}candidate",
        normalized,
    ):
        return True
    # Keep questions about status/listing in the read-only tool calling path.
    if not re.search(r"провер|исслед|обработ|верифиц|review|verify|check", normalized):
        return False
    try:
        active = await runtime.registry.get_active(user_id)
        async with asyncio.timeout(12):
            response = await runtime.registry.chat(user_id, active, (
                ChatMessage(role="system", content=(
                    "Классифицируй намерение пользователя. Ответь только одним токеном: "
                    "BULK_REVIEW, если он просит запустить проверку всех или группы "
                    "ожидающих кандидатов конференций; OTHER во всех иных случаях. "
                    "Ничего не выполняй. Вопросы о списке кандидатов = OTHER."
                )),
                ChatMessage(role="user", content=text[:600]),
            ))
        return response.content.strip().upper() == "BULK_REVIEW"
    except Exception:
        # Fallback to the normal tool-calling agent; no dropped requests.
        return False


