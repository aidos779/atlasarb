"""Throttle middleware — coarse per-user flood protection on all updates.

Complements the finer, feature-specific limits (search 20/min BR-SEARCH-2, refresh
1/3s BR-SIG-4) enforced inside handlers/services.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, User

from src.bot.callbacks import ack
from src.i18n import DEFAULT_LANGUAGE, t
from src.services.rate_limiter import SlidingWindowLimiter

# Bound on the remembered-language LRU. Only users who have actually passed through are
# held, and one short language code each, so this stays negligible next to the profile
# rows themselves.
_LANG_CACHE_MAX = 4096


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, max_events: int = 20, window_sec: float = 3.0) -> None:
        self._limiter = SlidingWindowLimiter(max_events, window_sec)
        # user_id -> language code, learned from requests that were *allowed* through.
        self._languages: OrderedDict[int, str] = OrderedDict()

    def _remember_language(self, user_id: int, data: dict[str, Any]) -> None:
        """Cache this user's language from a request that passed through.

        This middleware now runs ahead of ContextMiddleware, so ``data["profile"]`` is
        unset on the way *in* — but the two layers share one ``data`` dict, so the
        profile loaded downstream is visible here on the way back out. The limiter lets
        ``max_events`` requests through before it ever throttles, so by the time a user
        can be throttled their language has been learned.
        """
        profile = data.get("profile")
        if profile is None:
            return
        self._languages[user_id] = profile.settings.language.value
        self._languages.move_to_end(user_id)
        while len(self._languages) > _LANG_CACHE_MAX:
            self._languages.popitem(last=False)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user and not self._limiter.allow(tg_user.id):
            if isinstance(event, CallbackQuery):
                # Via ack() for the expiry guard: flood bursts are exactly when queries
                # sit past their answer window, so this is the likeliest place to be
                # answering an already-expired query.
                #
                # Localized from the remembered language rather than by loading the
                # profile: shedding the flood *without* the 8-query profile load is the
                # whole point of running ahead of ContextMiddleware.
                lang = self._languages.get(tg_user.id, DEFAULT_LANGUAGE)
                await ack(event, t("error.slow_down", lang), show_alert=False)
            return None
        result = await handler(event, data)
        if tg_user is not None:
            self._remember_language(tg_user.id, data)
        return result
