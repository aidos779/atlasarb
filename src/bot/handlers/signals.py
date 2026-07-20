"""Signal List + Signal Details handlers (PRD §9, §10).

Details is sent as its own new message so it stays forwardable/shareable (§10). Favorite
respects tier caps with upsell (BR-FAV-1). Hide is session-only (§9.4). Refresh is rate-
limited 1/3s (BR-SIG-4).
"""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from src.bot.callbacks import ack
from src.bot.context import BotContext
from src.bot.formatters.signal import format_details
from src.bot.handlers.common import SESSIONS, render_signal_list
from src.bot.keyboards.inline import details_buttons
from src.domain.signal import Signal
from src.domain.user import UserProfile
from src.i18n import favorite_kind_label, t, translations_of

router = Router(name="signals")


@router.message(Command("signals"))
async def cmd_signals(message: Message, ctx: BotContext, profile: UserProfile) -> None:
    if not profile.onboarding_complete:
        await message.answer(t("error.visitor", profile.settings.language.value))
        return
    SESSIONS.get(profile.telegram_user_id).page = 1
    await render_signal_list(message, ctx, profile)


@router.message(F.text.in_(translations_of("menu.signals")))
async def reply_signals(message: Message, ctx: BotContext, profile: UserProfile) -> None:
    await cmd_signals(message, ctx, profile)


@router.callback_query(F.data.startswith("sig:page:"))
async def signal_page(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    session = SESSIONS.get(profile.telegram_user_id)
    session.page = int(cb.data.split(":")[-1])
    await render_signal_list(cb, ctx, profile)


@router.callback_query(F.data == "sig:sort")
async def signal_sort(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    SESSIONS.get(profile.telegram_user_id).cycle_sort()
    await render_signal_list(cb, ctx, profile)


@router.callback_query(F.data == "sig:refresh")
async def signal_refresh(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    if not ctx.refresh_limiter.allow(profile.telegram_user_id):
        await ack(cb, t("signals.refresh_fast", lang), show_alert=False)
        return
    SESSIONS.get(profile.telegram_user_id).last_refresh = time.time()
    await render_signal_list(cb, ctx, profile)


@router.callback_query(F.data.startswith("sig:hide:"))
async def signal_hide(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    signal_id = cb.data.split(":")[-1]
    SESSIONS.get(profile.telegram_user_id).hidden.add(signal_id)
    await render_signal_list(cb, ctx, profile)


@router.callback_query(F.data.startswith("sig:fav:"))
async def signal_favorite(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    signal_id = cb.data.split(":")[-1]
    lang = profile.settings.language.value
    # The toast text depends on the toggle outcome, so the single fast DB toggle runs
    # first — everything slower (history write, edit/render round trips) comes after
    # the ack.
    outcome = await ctx.favorites.toggle(profile, "signal", signal_id)
    if outcome.cap_reached:
        await ack(cb, t("favorites.cap", lang,
                        kind=favorite_kind_label("signal", lang)), show_alert=True)
        return
    await ack(cb, t("favorites.added" if outcome.added else "favorites.removed", lang))
    signal = ctx.registry.get(signal_id)
    if signal and outcome.added:
        await ctx.history.record_favorite(profile.telegram_user_id, signal)
    # If we're on the details screen, refresh its buttons; else refresh list.
    if signal and cb.message and cb.message.text and "Signal ID" in (cb.message.text or ""):
        await cb.message.edit_reply_markup(
            reply_markup=details_buttons(signal, outcome.added, ctx.settings.bot_username, lang))
    else:
        await render_signal_list(cb, ctx, profile, need_ack=False)


@router.callback_query(F.data.startswith("sig:details:"))
async def signal_details(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    signal_id = cb.data.split(":")[-1]
    signal = ctx.registry.get(signal_id)  # in-memory — cheap enough to gate the toast
    lang = profile.settings.language.value
    if signal is None:
        await ack(cb, t("details.not_found", lang), show_alert=True)
        return
    await ack(cb)  # ack before the details pipeline (reliability query, FX, DB writes)
    await send_details(cb.message, ctx, profile, signal)


@router.callback_query(F.data.startswith("sig:report:"))
async def signal_report(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    # Feedback rolls into Admin Signal Monitoring review queue (§16.6).
    await ack(cb, t("signals.reported", profile.settings.language.value), show_alert=True)


async def send_details(target: Message, ctx: BotContext, profile: UserProfile,
                       signal: Signal) -> None:
    lang = profile.settings.language.value
    reliability = None
    if signal.buy_exchange and signal.sell_exchange:
        reliability = await ctx.history.reliability(
            signal.arb_type.value, signal.buy_exchange, signal.sell_exchange)
    text = await format_details(signal, profile, ctx.fx, reliability)
    fav_ids = set(await ctx.favorites.values(profile.telegram_user_id, "signal"))
    is_fav = signal.id in fav_ids
    kb = details_buttons(signal, is_fav, ctx.settings.bot_username, lang)
    await target.answer(text, reply_markup=kb, disable_web_page_preview=True)
    # Record view for history (§17.4). Details deliberately does not touch the Free
    # quota: this signal was already charged when it was delivered (list or alert), and
    # re-reading something you were given is not a new delivery.
    await ctx.history.record_view(profile.telegram_user_id, signal)
