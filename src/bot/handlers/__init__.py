"""Router aggregation. Order matters: specific routers first, fallback last."""
from __future__ import annotations

from aiogram import Dispatcher, F, Router
from aiogram.types import Message

from src.bot.handlers import (
    admin,
    favorites,
    filters,
    notifications,
    profile,
    search,
    settings,
    signals,
    start,
    subscription,
    support,
)
from src.bot.i18n import t
from src.domain.user import UserProfile

fallback = Router(name="fallback")


@fallback.message(F.text.startswith("/"))
async def unknown_command(message: Message, profile: UserProfile) -> None:
    await message.answer(t("error.unknown_command", profile.settings.language.value))


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(start.router)
    dp.include_router(signals.router)
    dp.include_router(search.router)
    dp.include_router(filters.router)
    dp.include_router(settings.router)
    dp.include_router(subscription.router)
    dp.include_router(favorites.router)
    dp.include_router(notifications.router)
    dp.include_router(profile.router)
    dp.include_router(support.router)
    dp.include_router(admin.router)
    dp.include_router(fallback)
