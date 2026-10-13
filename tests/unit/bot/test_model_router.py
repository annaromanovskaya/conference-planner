from types import SimpleNamespace

import pytest

from conference_scout.ai.models import AvailableModel
from conference_scout.ai.registry import ActiveModel, ProviderProfile
from conference_scout.bot.model_router import choose_model, list_model_profiles, list_profile_models


class State:
    def __init__(self):
        self.data = {}

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.data.clear()


class Target:
    def __init__(self):
        self.answers = []
        self.data = None
        self.message = self

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class Registry:
    def available_profiles(self):
        gateway = SimpleNamespace()
        return (ProviderProfile("local", "Локальный Ollama", gateway),)

    async def list_models(self, profile_id):
        assert profile_id == "local"
        return (AvailableModel(name="very/long:model-name" * 8),)

    async def select(self, user_id, profile_id, model_name):
        assert (user_id, profile_id) == (7, "local")
        if model_name != "very/long:model-name" * 8:
            raise AssertionError("stale model")
        return ActiveModel(profile_id, model_name, None)

    async def refresh_capability(self, _user_id):
        return ActiveModel("local", "very/long:model-name" * 8, False)


@pytest.mark.asyncio
async def test_model_selection_uses_short_indexed_callbacks_and_labels_chat_only() -> None:
    registry, state, target = Registry(), State(), Target()

    await list_model_profiles(target, registry, state)
    profile_callback = target.answers[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data
    assert len(profile_callback.encode()) < 64

    target.data = profile_callback
    await list_profile_models(target, registry, state)
    model_callback = target.answers[-2][1]["reply_markup"].inline_keyboard[0][0].callback_data
    assert len(model_callback.encode()) < 64

    target.data = model_callback
    await choose_model(target, registry, state, owner_user_id=7)
    assert "только чат" in target.answers[-2][0]
    assert "http" not in target.answers[-2][0]


@pytest.mark.asyncio
async def test_stale_model_index_is_rejected_before_selection() -> None:
    state, target = State(), Target()
    await state.update_data(profile_id="local", models=["one"])
    target.data = "model:pick:99"

    await choose_model(target, Registry(), state, owner_user_id=7)

    assert "устарел" in target.answers[-1][0].casefold()
