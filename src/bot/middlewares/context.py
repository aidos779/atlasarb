"""Context middleware — loads/creates the UserProfile and injects it + BotContext
into every handler's data (PRD §5, R-ROLE-1/2/3). Also blocks suspended users.

Runs on messages and callback queries. Onboarding enforcement is delegated to handlers
(some commands like /start, /help are available to Visitors, BR-HELP-1).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from src.bot.context import BotContext


class ContextMiddleware(BaseMiddleware):
    def __init__(self, context: BotContext) -> None:
        self._ctx = context

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        data["ctx"] = self._ctx
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        profile = await self._ctx.users.get_or_create(
            tg_user.id, tg_user.username, tg_user.first_name
        )
        data["profile"] = profile

        if profile.suspended:
            await self._notify_suspended(event)
            return None
        return await handler(event, data)

    @staticmethod
    async def _notify_suspended(event: TelegramObject) -> None:
        text = "⛔ Your account is suspended. Contact support."
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
