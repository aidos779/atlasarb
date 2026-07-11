"""Centralized handling of expired callback queries (audit item 10).

A user tapping a stale inline button raises TelegramBadRequest "query is too old…". The
global errors handler swallows exactly that (as a structured warning) and re-raises any
other TelegramBadRequest so genuine API misuse still surfaces.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from src.app import _on_telegram_bad_request


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
