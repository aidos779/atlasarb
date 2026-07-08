"""Throttle middleware — coarse per-user flood protection on all updates.

Complements the finer, feature-specific limits (search 20/min BR-SEARCH-2, refresh
1/3s BR-SIG-4) enforced inside handlers/services.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, User

from src.services.rate_limiter import SlidingWindowLimiter


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, max_events: int = 20, window_sec: float = 3.0) -> None:
        self._limiter = SlidingWindowLimiter(max_events, window_sec)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user and not self._limiter.allow(tg_user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Slow down a moment…", show_alert=False)
            return None
        return await handler(event, data)
