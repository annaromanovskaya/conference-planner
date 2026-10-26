from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from conference_planner.ai.gateway import ModelGatewayError
from conference_planner.ai.registry import ModelRegistry
from conference_planner.bot.keyboards import indexed_buttons


def build_model_router() -> Router:
    router = Router(name="model-selection")

    @router.message(Command("model"))
    async def model_command(message: Message, model_registry, state: FSMContext) -> None:
        await state.clear()
        await list_model_profiles(message, model_registry, state)

    @router.callback_query(F.data.startswith("model:profile:"))
    async def profile_callback(callback: CallbackQuery, model_registry, state: FSMContext) -> None:
        await list_profile_models(callback, model_registry, state)

    @router.callback_query(F.data.startswith("model:pick:"))
    async def model_callback(
        callback: CallbackQuery,
        model_registry,
        state: FSMContext,
        owner_user_id: int,
    ) -> None:
        await choose_model(callback, model_registry, state, owner_user_id=owner_user_id)

    return router


async def list_model_profiles(message: Message, registry: ModelRegistry, state) -> None:
    profiles = registry.available_profiles()
    values = [(profile.display_name, profile.id) for profile in profiles]
    if not values:
        await message.answer("Не настроено ни одного подключения к модели.")
        return
    await state.update_data(profiles=[value for _label, value in values])
    await message.answer(
        "Выберите подключение:",
        reply_markup=indexed_buttons(values, prefix="model:profile"),
    )


async def list_profile_models(callback: CallbackQuery, registry: ModelRegistry, state) -> None:
    data = await state.get_data()
    raw_index = (callback.data or "").rsplit(":", 1)[-1]
    profiles = data.get("profiles", [])
    if not raw_index.isdigit() or int(raw_index) >= len(profiles):
        await callback.answer("Список устарел. Выполните /model снова.", show_alert=True)
        return
    profile_id = profiles[int(raw_index)]
    try:
        models = await registry.list_models(profile_id)
    except ModelGatewayError:
        await callback.answer("Не удалось получить список моделей.", show_alert=True)
        return
    names = [model.name for model in models]
    await state.update_data(profile_id=profile_id, models=names)
    if not names:
        await callback.answer("Доступных моделей сейчас нет.", show_alert=True)
        return
    await callback.message.answer(
        "Выберите модель:",
        reply_markup=indexed_buttons([(name, name) for name in names], prefix="model:pick"),
    )
    await callback.answer()


async def choose_model(
    callback: CallbackQuery, registry: ModelRegistry, state, *, owner_user_id: int
) -> None:
    data = await state.get_data()
    raw_index = (callback.data or "").rsplit(":", 1)[-1]
    models = data.get("models", [])
    profile_id = data.get("profile_id")
    if not raw_index.isdigit() or int(raw_index) >= len(models) or not profile_id:
        await callback.answer("Список моделей устарел. Выполните /model снова.", show_alert=True)
        return
    model_name = models[int(raw_index)]
    try:
        await registry.select(owner_user_id, profile_id, model_name)
        active = await registry.refresh_capability(owner_user_id)
    except ModelGatewayError:
        await callback.answer("Модель недоступна. Обновите список через /model.", show_alert=True)
        return
    capability = "агент с инструментами" if active.supports_tools else "только чат"
    await callback.message.answer(f"Выбрана модель: {model_name}\nРежим: {capability}.")
    await state.clear()
    await callback.answer()
