"""Middleware execution order: ThrottleMiddleware ahead of ContextMiddleware.

Loading the profile costs 8 SQL round trips (4 idempotent INSERTs + a SELECT with 3
selectinloads). Running that ahead of the limiter meant a flood paid the full DB cost for
updates the limiter was about to drop. Throttling first makes a shed update cost zero
queries, and these tests pin that — plus the behaviour that must NOT change: normal
requests still load the profile, callbacks are still acknowledged, and the slow-down
notice is still localized.

The chain is exercised through a real Dispatcher via feed_update, so what is asserted is
the production wiring (including aiogram's own UserContextMiddleware, which supplies
`event_from_user` ahead of both of our middlewares) rather than a hand-built stand-in.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from src.bot.middlewares.context import ContextMiddleware
from src.bot.middlewares.throttle import ThrottleMiddleware
from src.i18n import t

USER_ID = 4242


class _CountingUsers:
    """Stands in for UserService, counting profile loads (the 8-query operation)."""

    def __init__(self, language: str = "en", suspended: bool = False) -> None:
        self.calls = 0
        self._profile = SimpleNamespace(
            telegram_user_id=USER_ID,
            suspended=suspended,
            settings=SimpleNamespace(language=SimpleNamespace(value=language)),
        )

    async def get_or_create(self, user_id, username, first_name):
        self.calls += 1
        return self._profile


def _build(users: _CountingUsers, max_events: int = 2):
    """Wire a dispatcher exactly as src/app.py does, and record handler invocations."""
    bot = Bot(token="42:TEST", default=DefaultBotProperties())
    # No network: every outgoing API call (notably answerCallbackQuery) resolves locally.
    bot.session = AsyncMock()
    bot.session.close = AsyncMock()

    dp = Dispatcher()
    ctx = SimpleNamespace(users=users)
    throttle = ThrottleMiddleware(max_events=max_events, window_sec=60.0)
    ctx_mw = ContextMiddleware(ctx)  # type: ignore[arg-type]
    for observer in (dp.message, dp.callback_query):
        observer.middleware(throttle)   # order under test
        observer.middleware(ctx_mw)

    seen: list[dict] = []

    @dp.callback_query()
    async def _handler(cb: CallbackQuery, **data):
        seen.append(data)

    return bot, dp, seen


def _update(update_id: int) -> Update:
    user = TgUser(id=USER_ID, is_bot=False, first_name="Test")
    chat = Chat(id=USER_ID, type="private")
    msg = Message(message_id=1, date=1700000000, chat=chat, from_user=user, text="hi")
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"q{update_id}", from_user=user, chat_instance="ci",
            data="menu:signals", message=msg,
        ),
    )


async def _feed(bot: Bot, dp: Dispatcher, n: int) -> None:
    for i in range(n):
        await dp.feed_update(bot, _update(i))


async def test_throttled_callbacks_never_touch_the_database():
    # Limiter allows 2; the following 6 are shed. Those 6 must cost zero profile loads.
    users = _CountingUsers()
    bot, dp, seen = _build(users, max_events=2)

    await _feed(bot, dp, 8)

    assert users.calls == 2, "throttled updates must not load the profile"
    assert len(seen) == 2, "throttled updates must not reach the handler"


async def test_normal_callbacks_still_load_the_profile():
    # Below the limit nothing changes: every update loads its profile and is handled,
    # with the profile injected into handler data as before.
    users = _CountingUsers()
    bot, dp, seen = _build(users, max_events=10)

    await _feed(bot, dp, 3)

    assert users.calls == 3
    assert len(seen) == 3
    assert all(d["profile"].telegram_user_id == USER_ID for d in seen)
    assert all(d["ctx"] is not None for d in seen)


async def test_throttled_callback_is_still_acknowledged():
    # The flood notice still answers the query, so the user's button never hangs.
    users = _CountingUsers()
    bot, dp, _ = _build(users, max_events=1)

    await _feed(bot, dp, 3)

    methods = [c.args[1] for c in bot.session.await_args_list if len(c.args) > 1]
    answers = [m for m in methods if type(m).__name__ == "AnswerCallbackQuery"]
    assert len(answers) == 2, "each shed callback must still be acknowledged"


async def test_throttle_notice_stays_localized():
    # The regression the reorder could have caused: with the profile no longer loaded
    # on the way in, a non-English user must still get their own language — learned
    # from the requests that were allowed through.
    users = _CountingUsers(language="ru")
    bot, dp, _ = _build(users, max_events=1)

    await _feed(bot, dp, 3)

    methods = [c.args[1] for c in bot.session.await_args_list if len(c.args) > 1]
    answers = [m for m in methods if type(m).__name__ == "AnswerCallbackQuery"]
    assert answers, "expected a throttle notice"
    assert answers[-1].text == t("error.slow_down", "ru")
    assert answers[-1].text != t("error.slow_down", "en"), "must not fall back to default"


async def test_unknown_user_falls_back_to_default_language():
    # A user throttled before any request completed has no remembered language — the
    # documented DEFAULT_LANGUAGE fallback, unchanged from before the reorder.
    mw = ThrottleMiddleware(max_events=0, window_sec=60.0)
    cb = MagicMock(spec=CallbackQuery)
    cb.data = "menu:signals"
    cb.answer = AsyncMock()
    handler = AsyncMock()

    await mw(handler, cb, {"event_from_user": TgUser(id=9, is_bot=False, first_name="X")})

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.args[0] == t("error.slow_down", "en")
    handler.assert_not_awaited()
