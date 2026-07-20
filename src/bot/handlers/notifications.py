"""Notifications screen handlers (PRD §13) — toggles, pause, nudge; some non-disableable."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery

from src.bot.callbacks import ack
from src.bot.context import BotContext
from src.bot.keyboards.screens import notifications_menu
from src.database.repositories.misc_repos import NotificationRepository
from src.domain.entitlements import entitlements_for
from src.domain.user import UserProfile
from src.i18n import t, tier_label

router = Router(name="notifications")


async def _show(cb: CallbackQuery, profile: UserProfile, need_ack: bool = True) -> None:
    lang = profile.settings.language.value
    ent = entitlements_for(profile.effective_tier)
    s = profile.settings
    text = t("notifications.title", lang)
    if not s.instant_alerts_enabled and not s.favorite_coin_alerts:
        text += "\n\n" + t("notifications.nudge", lang)  # BR-NOTIF-3
    if need_ack:
        await ack(cb)
    await cb.message.edit_text(text, reply_markup=notifications_menu(profile, ent, lang))


@router.callback_query(F.data == "menu:notifications")
async def menu_notifications(cb: CallbackQuery, profile: UserProfile) -> None:
    await _show(cb, profile)


@router.callback_query(F.data.startswith("notif:toggle:"))
async def toggle(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    which = cb.data.split(":")[-1]
    ent = entitlements_for(profile.effective_tier)
    lang = profile.settings.language.value
    s = profile.settings
    if which in ("fav_coin", "fav_exchange") and not ent.favorite_entity_alerts:
        await ack(cb, t("filters.upsell", lang,
                        filter=t("filters.favorite_alerts", lang),
                        tier=tier_label("basic", lang)), show_alert=True)
        return
    await ack(cb)
    if which == "instant":
        s.instant_alerts_enabled = not s.instant_alerts_enabled
    elif which == "daily":
        s.daily_summary_enabled = not s.daily_summary_enabled
    elif which == "fav_coin":
        s.favorite_coin_alerts = not s.favorite_coin_alerts
    elif which == "fav_exchange":
        s.favorite_exchange_alerts = not s.favorite_exchange_alerts
    await ctx.users.save(profile)
    await _show(cb, profile, need_ack=False)


@router.callback_query(F.data.startswith("alert:mute:"))
async def mute_pair(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    signal_id = cb.data.split(":")[-1]
    signal = ctx.registry.get(signal_id)  # in-memory lookup — cheap
    lang = profile.settings.language.value
    if signal is None:
        await ack(cb)
        return
    # Toast content is known before the DB write — ack first.
    await ack(cb, t("alert.muted", lang, pair=signal.trading_pair), show_alert=False)
    until = datetime.now(UTC) + timedelta(hours=1)  # §13.5 mute 1h
    async with ctx.database.session() as session:
        await NotificationRepository(session).mute_pair(
            profile.telegram_user_id, signal.trading_pair, until)


@router.callback_query(F.data.startswith("notif:pause:"))
async def pause(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    hours = int(cb.data.split(":")[-1])
    await ack(cb, t("notifications.paused", profile.settings.language.value, hours=hours),
              show_alert=False)
    until = datetime.now(UTC) + timedelta(hours=hours)
    # Global pause = mute all active pairs; modeled via a wildcard mute row.
    async with ctx.database.session() as session:
        await NotificationRepository(session).mute_pair(
            profile.telegram_user_id, "*ALL*", until)
    await _show(cb, profile, need_ack=False)
