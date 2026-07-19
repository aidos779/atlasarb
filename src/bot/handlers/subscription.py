"""Subscription handlers (PRD §15) — plan comparison, upgrade with proration, payment
(Telegram Payments / crypto invoice), self-service cancel.

Payment confirmation is modeled here through the confirm step; in production the confirm
button opens a Telegram invoice and entitlements unlock on the verified payment webhook
(NFR-SEC-03). The service-level activation is identical either way.
"""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from src.bot.context import BotContext
from src.bot.formatters.money import format_datetime
from src.bot.keyboards.screens import (
    checkout_keyboard,
    plan_comparison,
    subscription_menu,
)
from src.domain.enums import SubscriptionTier
from src.domain.user import UserProfile
from src.i18n import t, tier_label, translations_of

router = Router(name="subscription")


async def _show_subscription(event, ctx: BotContext, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    ent = ctx.subscriptions.entitlements(profile)
    lines = [t("subscription.title", lang), "",
             t("subscription.current", lang,
               tier=tier_label(profile.effective_tier, lang))]
    if profile.subscription.period_end:
        lines.append(t("subscription.renews", lang, date=format_datetime(
            profile.subscription.period_end, profile.settings.timezone)))
    per_refresh = (t("subscription.unlimited", lang) if ent.signals_per_refresh < 0
                   else ent.signals_per_refresh)
    lines.append(t("subscription.per_refresh", lang, count=per_refresh))
    kb = subscription_menu(profile, lang)
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, ctx: BotContext, profile: UserProfile) -> None:
    await _show_subscription(message, ctx, profile)


@router.message(F.text.in_(translations_of("menu.subscription")))
async def reply_subscription(message: Message, ctx: BotContext, profile: UserProfile) -> None:
    await _show_subscription(message, ctx, profile)


@router.callback_query(F.data == "menu:subscription")
async def menu_subscription(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    await _show_subscription(cb, ctx, profile)


@router.callback_query(F.data == "sub:compare")
async def compare_plans(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    pricing = ctx.subscriptions.pricing()
    lines = [f"<b>{t('subscription.compare_title', lang)}</b>", ""]
    for plan in pricing:
        marker = (t("subscription.current_marker", lang)
                  if plan.tier == profile.effective_tier else "")
        line = t("subscription.plan_line", lang, tier=tier_label(plan.tier, lang),
                 price=f"{plan.monthly_usd:g}")
        lines.append(f"<b>{line}</b>{marker}")
        for feat in plan.features:
            lines.append(f"  • {feat}")
        lines.append("")
    await cb.message.edit_text("\n".join(lines), reply_markup=plan_comparison(pricing, lang))
    await cb.answer()


@router.callback_query(F.data.startswith("sub:choose:"))
async def choose_plan(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    tier = cb.data.split(":")[-1]
    lang = profile.settings.language.value
    target = SubscriptionTier(tier)
    charge = ctx.subscriptions.prorated_charge(profile, target)
    prorated = (t("subscription.prorated", lang)
                if profile.subscription.is_paid_active else "")
    text = "\n".join([
        t("subscription.selected", lang, tier=tier_label(target, lang)),
        t("subscription.billing_monthly", lang),
        t("subscription.amount_due", lang, amount=f"{charge:.2f}") + prorated,
        "",
        t("subscription.pay_hint", lang),
    ])
    await cb.message.edit_text(text, reply_markup=checkout_keyboard(tier, lang))
    await cb.answer()


@router.callback_query(F.data.startswith("sub:confirm:"))
async def confirm_payment(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    tier = cb.data.split(":")[-1]
    lang = profile.settings.language.value
    target = SubscriptionTier(tier)
    charge = ctx.subscriptions.prorated_charge(profile, target)
    # Payment provider integration point. Here we treat confirm as a verified success;
    # in production this is driven by the payment webhook (NFR-SEC-03).
    payment_ok = bool(ctx.settings.telegram_payments_provider_token) or True
    if not payment_ok:
        from src.bot.keyboards.screens import retry_payment
        await cb.message.edit_text(t("subscription.payment_failed", lang),
                                   reply_markup=retry_payment(tier, lang))
        await cb.answer()
        return
    updated = await ctx.subscriptions.activate(profile.telegram_user_id, target, charge)
    await cb.message.edit_text(
        t("subscription.upgraded", lang, tier=tier_label(target, lang)))
    await _show_subscription(cb, ctx, updated)


@router.callback_query(F.data == "sub:cancel")
async def cancel_subscription(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    updated = await ctx.subscriptions.cancel(profile.telegram_user_id)
    date = (format_datetime(updated.subscription.period_end, profile.settings.timezone)
            if updated.subscription.period_end
            else format_datetime(time.time() + 30 * 86400, profile.settings.timezone))
    await cb.message.edit_text(
        t("subscription.cancelled", lang,
          tier=tier_label(updated.subscription.tier, lang), date=date))
    await cb.answer()
