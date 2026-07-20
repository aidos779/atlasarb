"""Callback-query acknowledgement helper.

Telegram gives a callback query a short answer window (~15s) and shows a spinner on the
user's button until answerCallbackQuery is called. Two failure modes produced the
"query is too old / query id is invalid" noise in production:

  - late ack: handlers edited the message (a full Bot API round trip, sometimes several)
    *before* answering, so a slow network or an exception in the edit left the query
    unanswered entirely;
  - double ack: handlers that toasted and then delegated to a render helper answered the
    same query twice — the second answer *always* fails with QUERY_ID_INVALID.

Contract: every callback handler acknowledges exactly once, as early as possible, through
``ack()``. Expiry/duplicate errors are expected timing conditions — logged as a structured
warning, never a traceback — and the handler keeps going (a late tap should still get its
screen update). Any other TelegramBadRequest propagates unchanged.
"""
from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from src.config import get_logger

log = get_logger("bot.callbacks")

# Answers that lose the Telegram window (user tapped a stale button, or the query was
# already answered) raise TelegramBadRequest with one of these fragments.
CALLBACK_EXPIRED_MARKERS = ("query is too old", "query id is invalid", "query is invalid")


def is_expired_callback_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in CALLBACK_EXPIRED_MARKERS)


async def ack(cb: CallbackQuery, text: str | None = None, show_alert: bool = False) -> bool:
    """Answer the callback query, tolerating expiry.

    Returns True if Telegram accepted the answer, False if the query was already
    expired/answered (benign — logged as a warning, no traceback). Unrelated
    TelegramBadRequest errors are re-raised so genuine API misuse still surfaces.
    """
    try:
        await cb.answer(text, show_alert=show_alert)
        return True
    except TelegramBadRequest as exc:
        if is_expired_callback_error(exc):
            log.warning("callback_query_expired", callback_data=cb.data,
                        error=str(exc))
            return False
        raise
