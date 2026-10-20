from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conference_scout.ai.registry import ProviderProfile
from conference_scout.ai.usage import ModelUsageView, OllamaUsageSnapshot
from conference_scout.bot.keyboards import ollama_usage_keyboard
from conference_scout.bot.usage_router import (
    CycleScheduleView,
    choose_usage_model,
    format_usage_snapshot,
    refresh_usage,
    show_usage,
)
from conference_scout.db.repositories.ollama_usage import UsageAggregate
from conference_scout.runtime_clock import ApplicationClock
from conference_scout.schemas.common import ProviderUsageStatus


def _snapshot(
    *,
    status: ProviderUsageStatus = ProviderUsageStatus.AVAILABLE,
    active_model: str | None = "gemma4:31b",
    available_models: tuple[str, ...] = ("gemma4:31b",),
    models: tuple[ModelUsageView, ...] | None = None,
) -> OllamaUsageSnapshot:
    return OllamaUsageSnapshot(
        active_profile_id="ollama-cloud",
        active_model_name=active_model,
        models=models
        if models is not None
        else (
            ModelUsageView(
                model_name="gemma4:31b",
                today=UsageAggregate(2, 120, 80, 25),
                month=UsageAggregate(5, 400, 200, 90),
                observed=True,
            ),
            ModelUsageView(
                model_name="gpt-oss:<20b>",
                today=UsageAggregate(0, 0, 0, 0),
                month=UsageAggregate(0, 0, 0, 0),
                observed=False,
            ),
        ),
        status=status,
        status_model_name=active_model,
        status_observed_at=datetime(2026, 10, 14, 12, 34, tzinfo=UTC),
        detail_code=None,
        available_models=available_models,
        generated_at=datetime(2026, 10, 14, 12, 35, tzinfo=UTC),
    )


class State:
    def __init__(self) -> None:
        self.cleared = 0
        self.data = {}

    async def clear(self) -> None:
        self.cleared += 1
        self.data.clear()

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)


class Target:
    def __init__(self) -> None:
        self.answers = []
        self.message = self

    async def answer(self, text=None, **kwargs) -> None:
        self.answers.append((text, kwargs))


class UsageService:
    def __init__(self, snapshot: OllamaUsageSnapshot) -> None:
        self.value = snapshot
        self.calls = []

    async def snapshot(self, user_id: int, *, refresh_models: bool):
        self.calls.append((user_id, refresh_models))
        return self.value


def test_usage_formatter_is_truthful_and_escapes_model_names() -> None:
    text = format_usage_snapshot(_snapshot())

    assert "OLLAMA FREE USAGE" in text
    assert "ollama-cloud / gemma4:31b" in text
    assert "доступен" in text
    assert "2026-10-14 12:34 UTC" in text
    assert "Сегодня: запросы 2; вход 120; кеш 80; выход 25" in text
    assert "За календарный месяц (локально): запросы 5; вход 400; кеш 200; выход 90" in text
    assert "gpt-oss:&lt;20b&gt;" in text
    assert "Точный Free usage доступен только в Ollama Settings" in text


def test_usage_formatter_shows_local_runtime_clock() -> None:
    instant = datetime(2026, 10, 19, 8, 30, tzinfo=UTC)
    clock = ApplicationClock.start(
        ZoneInfo("Europe/Moscow"), now_provider=lambda: instant
    )

    text = format_usage_snapshot(
        _snapshot(),
        clock=clock,
        cycle_schedule=CycleScheduleView(
            last_run_id=12,
            last_status="COMPLETED",
            last_finished_at=datetime(2026, 10, 19, 7, 15, tzinfo=UTC),
            next_run_at=datetime(2026, 10, 26, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        ),
    )

    assert "Временная зона: Europe/Moscow" in text
    assert "Запущен: 2026-10-19 11:30" in text
    assert "Текущее время: 2026-10-19 11:30" in text
    assert "Поиск конференций: начиная с 2026-10-19" in text
    assert "Последний цикл: №12, завершён" in text
    assert "2026-10-19 10:15" in text
    assert "Следующий цикл: понедельник, 2026-10-26 10:00" in text


def test_usage_formatter_does_not_invent_balance_when_no_data_exists() -> None:
    snapshot = _snapshot(
        status=ProviderUsageStatus.UNKNOWN,
        models=(
            ModelUsageView(
                model_name="gemma4:31b",
                today=UsageAggregate(0, 0, 0, 0),
                month=UsageAggregate(0, 0, 0, 0),
                observed=False,
            ),
        ),
    )

    text = format_usage_snapshot(snapshot)

    assert "Локальных наблюдений пока нет" in text
    assert "%" not in text
    assert "осталось" not in text.casefold()
    assert "Баланс аккаунта:" not in text
    assert "не являются балансом аккаунта" in text


@pytest.mark.parametrize(
    ("status", "available", "active", "expected_choice"),
    [
        (ProviderUsageStatus.AVAILABLE, ("gemma4:31b", "gpt-oss:20b"), "gemma4:31b", False),
        (ProviderUsageStatus.RATE_LIMITED, ("gemma4:31b",), "gemma4:31b", False),
        (
            ProviderUsageStatus.RATE_LIMITED,
            ("gemma4:31b", "gpt-oss:20b"),
            "gemma4:31b",
            True,
        ),
        (
            ProviderUsageStatus.FREE_EXHAUSTED,
            ("gpt-oss:20b",),
            "gemma4:31b",
            True,
        ),
    ],
)
def test_usage_keyboard_offers_only_explicit_free_model_switch(
    status, available, active, expected_choice
) -> None:
    keyboard = ollama_usage_keyboard(
        offer_model_choice=(
            status in {ProviderUsageStatus.RATE_LIMITED, ProviderUsageStatus.FREE_EXHAUSTED}
            and any(model != active for model in available)
        )
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert buttons[0].text == "Обновить статистику"
    assert buttons[0].callback_data == "usage:refresh"
    assert buttons[1].url == "https://ollama.com/settings/usage"
    assert any(button.callback_data == "usage:model" for button in buttons) is expected_choice


@pytest.mark.asyncio
async def test_settings_and_refresh_render_current_snapshot() -> None:
    service = UsageService(_snapshot())
    state, message, callback = State(), Target(), Target()

    await show_usage(message, state, service, owner_user_id=7)
    await refresh_usage(callback, service, owner_user_id=7)

    assert state.cleared == 1
    assert service.calls == [(7, True), (7, True)]
    assert "OLLAMA FREE USAGE" in message.answers[0][0]
    assert "OLLAMA FREE USAGE" in callback.answers[0][0]
    assert callback.answers[-1][0] is None


@pytest.mark.asyncio
async def test_usage_model_button_clears_state_and_opens_existing_selection() -> None:
    class Registry:
        def available_profiles(self):
            return (ProviderProfile("ollama-cloud", "Ollama Cloud", SimpleNamespace()),)

    state, callback = State(), Target()

    await choose_usage_model(callback, state, Registry())

    assert state.cleared == 1
    assert state.data == {"profiles": ["ollama-cloud"]}
    assert callback.answers[0][0] == "Выберите подключение:"
    assert callback.answers[-1][0] is None
