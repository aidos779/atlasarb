"""Notifications screen handlers (PRD §13) — toggles, pause, nudge; some non-disableable."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery

from src.bot.context import BotContext
from src.bot.i18n import t
from src.bot.keyboards.screens import notifications_menu
from src.database.repositories.misc_repos import NotificationRepository
from src.domain.entitlements import entitlements_for
from src.domain.user import UserProfile

router = Router(name="notifications")


async def _show(cb: CallbackQuery, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    ent = entitlements_for(profile.effective_tier)
    s = profile.settings
    text = t("notifications.title", lang)
    if not s.instant_alerts_enabled and not s.favorite_coin_alerts:
        text += "\n\n" + t("notifications.nudge", lang)  # BR-NOTIF-3
    await cb.message.edit_text(text, reply_markup=notifications_menu(profile, ent, lang))
    await cb.answer()


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
        await cb.answer(t("filters.upsell", lang, filter="Favorite alerts", tier="Basic"),
                        show_alert=True)
        return
    if which == "instant":
        s.instant_alerts_enabled = not s.instant_alerts_enabled
    elif which == "daily":
        s.daily_summary_enabled = not s.daily_summary_enabled
    elif which == "fav_coin":
        s.favorite_coin_alerts = not s.favorite_coin_alerts
    elif which == "fav_exchange":
        s.favorite_exchange_alerts = not s.favorite_exchange_alerts
    await ctx.users.save(profile)
    await _show(cb, profile)


@router.callback_query(F.data.startswith("alert:mute:"))
async def mute_pair(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    signal_id = cb.data.split(":")[-1]
    signal = ctx.registry.get(signal_id)
    lang = profile.settings.language.value
    if signal is None:
        await cb.answer()
        return
    until = datetime.now(UTC) + timedelta(hours=1)  # §13.5 mute 1h
    async with ctx.database.session() as session:
        await NotificationRepository(session).mute_pair(
            profile.telegram_user_id, signal.trading_pair, until)
    await cb.answer(t("alert.muted", lang, pair=signal.trading_pair), show_alert=False)


@router.callback_query(F.data.startswith("notif:pause:"))
async def pause(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    hours = int(cb.data.split(":")[-1])
    until = datetime.now(UTC) + timedelta(hours=hours)
    # Global pause = mute all active pairs; modeled via a wildcard mute row.
    async with ctx.database.session() as session:
        await NotificationRepository(session).mute_pair(
            profile.telegram_user_id, "*ALL*", until)
    await cb.answer(f"🔕 Paused all alerts for {hours}h", show_alert=False)
    await _show(cb, profile)
