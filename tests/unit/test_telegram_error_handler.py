"""Centralized handling of expired callback queries (audit item 10).

A user tapping a stale inline button raises TelegramBadRequest "query is too old…". The
global errors handler swallows exactly that (as a structured warning) and re-raises any
other TelegramBadRequest so genuine API misuse still surfaces.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from src.app import _on_telegram_bad_request
from src.bot.middlewares.context import ContextMiddleware
from src.bot.middlewares.throttle import ThrottleMiddleware


def _event(message: str) -> SimpleNamespace:
    return SimpleNamespace(exception=TelegramBadRequest(method=None, message=message))


async def test_expired_callback_query_is_swallowed():
    handled = await _on_telegram_bad_request(
        _event("query is too old and response timeout expired or query ID is invalid"))
    assert handled is True


async def test_invalid_query_id_is_swallowed():
    handled = await _on_telegram_bad_request(_event("query ID is invalid"))
    assert handled is True


async def test_other_bad_request_is_reraised():
    # A real API misuse (not a stale query) must NOT be silently swallowed.
    with pytest.raises(TelegramBadRequest):
        await _on_telegram_bad_request(_event("message text is empty"))


# ── src.bot.callbacks.ack: the per-handler ack path (handlers no longer call
# cb.answer directly, so the suppression contract is enforced here too) ──

class _FakeCb:
    """Minimal CallbackQuery stand-in recording answer() calls."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.data = "sig:page:2"
        self.calls: list[tuple] = []
        self._raises = raises

    async def answer(self, text=None, show_alert=False):
        self.calls.append((text, show_alert))
        if self._raises is not None:
            raise self._raises


async def test_ack_returns_true_and_forwards_toast():
    from src.bot.callbacks import ack
    cb = _FakeCb()
    assert await ack(cb, "saved", show_alert=True) is True
    assert cb.calls == [("saved", True)]


async def test_ack_swallows_expired_query_and_returns_false():
    # A stale button tap must not raise out of the handler — the handler keeps going
    # and still renders the screen; the failure is a structured warning, not a traceback.
    from src.bot.callbacks import ack
    cb = _FakeCb(TelegramBadRequest(
        method=None,
        message="query is too old and response timeout expired or query ID is invalid"))
    assert await ack(cb) is False


async def test_ack_reraises_unrelated_bad_request():
    # Only the expiry/duplicate family is suppressed — real API misuse still surfaces.
    from src.bot.callbacks import ack
    cb = _FakeCb(TelegramBadRequest(method=None, message="message text is empty"))
    with pytest.raises(TelegramBadRequest):
        await ack(cb)


# ── middleware ack paths: both middlewares answer callback queries *outside* any
# handler, so a stale tap there had no guard at all until they routed through ack() ──

def _spec_cb(raises: Exception | None = None) -> MagicMock:
    """A CallbackQuery-typed mock so the middlewares' isinstance() checks match."""
    cb = MagicMock(spec=CallbackQuery)
    cb.data = "menu:signals"
    cb.answer = AsyncMock(side_effect=raises)
    return cb


async def test_suspended_notice_survives_expired_query():
    # A suspended user tapping a stale button: the notice attempt must not raise out of
    # ContextMiddleware, which sits above every handler.
    cb = _spec_cb(TelegramBadRequest(method=None, message="query is too old"))
    await ContextMiddleware._notify_suspended(cb, "en")
    cb.answer.assert_awaited_once()


async def test_throttle_notice_survives_expired_query():
    # Flood bursts are exactly when queries expire, so the slow-down toast is a likely
    # place to be answering an already-dead query.
    mw = ThrottleMiddleware(max_events=1, window_sec=60.0)
    user = SimpleNamespace(id=777, is_bot=False)
    handler = AsyncMock()
    cb = _spec_cb(TelegramBadRequest(method=None, message="query is too old"))

    # First call passes through to the handler; the second trips the limiter and takes
    # the answer path, where the expired-query error must be swallowed.
    await mw(handler, cb, {"event_from_user": user})
    handler.assert_awaited_once()
    await mw(handler, cb, {"event_from_user": user})
    cb.answer.assert_awaited_once()
    handler.assert_awaited_once()  # throttled call never reached the handler


async def test_throttle_notice_reraises_unrelated_bad_request():
    mw = ThrottleMiddleware(max_events=1, window_sec=60.0)
    user = SimpleNamespace(id=778, is_bot=False)
    handler = AsyncMock()
    cb = _spec_cb(TelegramBadRequest(method=None, message="message text is empty"))

    await mw(handler, cb, {"event_from_user": user})
    with pytest.raises(TelegramBadRequest):
        await mw(handler, cb, {"event_from_user": user})
