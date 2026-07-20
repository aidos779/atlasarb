"""Context middleware — loads/creates the UserProfile and injects it + BotContext
into every handler's data (PRD §5, R-ROLE-1/2/3). Also blocks suspended users.

Runs on messages and callback queries, immediately inside ThrottleMiddleware — the
profile load is the expensive step (8 SQL round trips), so flood shedding happens ahead
of it and a throttled update never reaches this middleware at all.

Onboarding enforcement is delegated to handlers (some commands like /start, /help are
available to Visitors, BR-HELP-1).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from src.bot.callbacks import ack
from src.bot.context import BotContext
from src.i18n import t


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
            await self._notify_suspended(event, profile.settings.language.value)
            return None
        return await handler(event, data)

    @staticmethod
    async def _notify_suspended(event: TelegramObject, lang: str) -> None:
        text = t("error.suspended", lang)
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            # Same one-ack contract as the handlers (src/bot/callbacks.py): a suspended
            # user tapping a stale button must not raise "query is too old" out of the
            # middleware, where no handler-level guard would catch it.
            await ack(event, text, show_alert=True)
