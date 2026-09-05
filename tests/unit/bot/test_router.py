from aiogram import Router

from conference_scout.bot.router import build_router


def test_router_registers_owner_management_flows() -> None:
    router = build_router(owner_telegram_id=123)
    assert isinstance(router, Router)
    assert router.message.handlers
    assert router.callback_query.handlers
