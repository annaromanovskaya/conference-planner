from types import SimpleNamespace

import pytest
from aiogram import Router

from conference_scout.bot import router as bot_router
from conference_scout.bot.router import build_router


def test_router_registers_owner_management_flows() -> None:
    router = build_router(owner_telegram_id=123)
    assert isinstance(router, Router)
    assert router.message.handlers
    assert router.callback_query.handlers
    assert "ollama-usage" in [child.name for child in router.sub_routers]
    assert "conference-views" in [child.name for child in router.sub_routers]
    assert [child.name for child in router.sub_routers][-2:] == [
        "agent-chat",
        "callback-fallback",
    ]


def test_flow_input_does_not_capture_bot_commands() -> None:
    assert bot_router.is_flow_text("астрономия") is True
    assert bot_router.is_flow_text("/model") is False
    assert bot_router.is_flow_text("  /help") is False
    assert bot_router.is_flow_text("") is False


def test_flow_input_does_not_capture_any_main_menu_button() -> None:
    for label in bot_router.MAIN_MENU_BUTTONS:
        assert bot_router.is_flow_text(label) is False


@pytest.mark.parametrize(
    "label",
    ["Дальше", "Следующий кандидат", "начинай верификацию", "давай дальше"],
)
def test_candidate_queue_commands_bypass_chat_agent(label: str) -> None:
    assert bot_router.is_next_candidate_text(label) is True
    assert bot_router.is_flow_text(label) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.org/events", ("PAGE", "https://example.org/events")),
        ("PAGE https://example.org/events", ("PAGE", "https://example.org/events")),
        ("domain iau.org", ("DOMAIN", "iau.org")),
        ("iau.org", ("DOMAIN", "iau.org")),
        ("not a source", None),
    ],
)
def test_source_input_accepts_bare_url_and_domain(raw, expected) -> None:
    parsed = bot_router.parse_source_input(raw)

    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert (parsed[0].value, parsed[1]) == expected


@pytest.mark.asyncio
async def test_opening_menu_clears_stale_input_state() -> None:
    class State:
        cleared = False

        async def clear(self):
            self.cleared = True

    state = State()

    await bot_router.open_menu(state)

    assert state.cleared is True


@pytest.mark.asyncio
async def test_cancel_command_clears_flow_state() -> None:
    class State:
        cleared = False

        async def clear(self):
            self.cleared = True

    class Message:
        def __init__(self):
            self.answers = []

        async def answer(self, text, **kwargs):
            self.answers.append((text, kwargs))

    state, message = State(), Message()

    await bot_router.cancel_flow(message, state)

    assert state.cleared is True
    assert "отмен" in message.answers[0][0].casefold()
    assert message.answers[0][1]["reply_markup"].is_persistent is True


@pytest.mark.asyncio
async def test_topics_renders_ampersand_through_html() -> None:
    class State:
        async def clear(self):
            return None

    class Message:
        def __init__(self):
            self.answers = []

        async def answer(self, text, **kwargs):
            self.answers.append((text, kwargs))

    class TopicRepository:
        owner_user_id = 123

        async def list_for_user(self, user_id):
            assert user_id == 123
            return [SimpleNamespace(name="Astro-ML & Stellar Physics")]

    router = build_router(owner_telegram_id=123)
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "topics")
    message = Message()

    await handler(message, State(), TopicRepository())

    text, kwargs = message.answers[0]
    assert text == "⭐ Astro-ML &amp; Stellar Physics"
    assert kwargs["parse_mode"] == "HTML"
