"""Subscription handlers — plan comparison and the Pro Lifetime checkout entry point.

Pro is a one-time purchase, so there is no upgrade path, no proration and no
cancellation flow to render: a user is either Free (quota-capped) or Pro (forever).
Every price shown here comes from the product catalogue, never from a literal.

``sub:buy`` runs the real purchase lifecycle (PurchaseService) and shows whatever the
configured provider returns. With the placeholder provider no invoice can be issued, so
the checkout closes as CANCELLED and the user sees "coming soon" — the grant itself only
ever happens on a verified settlement (see src/services/payments/provider.py).
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from src.bot.callbacks import ack
from src.bot.context import BotContext
from src.bot.formatters.money import format_datetime
from src.bot.keyboards.inline import back_home
from src.bot.keyboards.screens import checkout_keyboard, plan_comparison, subscription_menu
from src.domain.user import UserProfile
from src.i18n import t, tier_label, translations_of

router = Router(name="subscription")


async def show_subscription_screen(event, ctx: BotContext, profile: UserProfile,
                                   need_ack: bool = True) -> None:
    if need_ack and isinstance(event, CallbackQuery):
        await ack(event)
    lang = profile.settings.language.value
    product = ctx.subscriptions.pro_product()
    lines = [t("subscription.title", lang), "",
             t("subscription.current", lang,
               tier=tier_label(profile.effective_tier, lang))]
    if profile.subscription.is_pro:
        lines.append(t("subscription.lifetime_active", lang))
        if profile.subscription.purchased_at:
            lines.append(t("subscription.purchased_on", lang, date=format_datetime(
                profile.subscription.purchased_at, profile.settings.timezone)))
    else:
        allowance = await ctx.signal_access.allowance(profile)
        lines.append(t("subscription.free_usage", lang, used=allowance.delivered,
                       quota=allowance.quota, remaining=allowance.remaining))
    kb = subscription_menu(profile, lang, product.amount_float)
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, ctx: BotContext, profile: UserProfile) -> None:
    await show_subscription_screen(message, ctx, profile)


@router.message(F.text.in_(translations_of("menu.subscription")))
async def reply_subscription(message: Message, ctx: BotContext, profile: UserProfile) -> None:
    await show_subscription_screen(message, ctx, profile)


@router.callback_query(F.data == "menu:subscription")
async def menu_subscription(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    await show_subscription_screen(cb, ctx, profile)


@router.callback_query(F.data == "sub:compare")
async def compare_plans(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    await ack(cb)
    lang = profile.settings.language.value
    pricing = ctx.subscriptions.pricing()
    lines = [f"<b>{t('subscription.compare_title', lang)}</b>", ""]
    for plan in pricing:
        marker = (t("subscription.current_marker", lang)
                  if plan.tier == profile.effective_tier else "")
        key = "subscription.plan_line_once" if plan.one_time else "subscription.plan_line_free"
        line = t(key, lang, tier=tier_label(plan.tier, lang), price=f"{plan.price_usd:g}")
        lines.append(f"<b>{line}</b>{marker}")
        for feat in plan.features:
            lines.append(f"  • {feat}")
        lines.append("")
    await cb.message.edit_text("\n".join(lines), reply_markup=plan_comparison(pricing, lang))


@router.callback_query(F.data == "sub:buy")
async def buy_pro(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    """Checkout entry point — opens a purchase and asks the provider for an invoice.

    The full lifecycle already runs here: a Purchase row is created (or an abandoned
    one resumed) and moves to PENDING once an invoice exists. With the placeholder
    provider no invoice can be issued, so the purchase is closed as CANCELLED and the
    user sees "coming soon" — nothing is granted, and no payment is faked.
    """
    await ack(cb)
    lang = profile.settings.language.value
    if profile.subscription.is_pro:
        await cb.message.edit_text(t("subscription.already_pro", lang))
        return
    product = ctx.subscriptions.pro_product()
    checkout = await ctx.purchases.start_checkout(profile.telegram_user_id, product)
    if not checkout.available:
        text = "\n\n".join([
            t("subscription.checkout_title", lang, price=product.format_amount()),
            t("subscription.checkout_soon", lang),
        ])
        await cb.message.edit_text(text,
                                   reply_markup=back_home(lang, back="menu:subscription"))
        return
    text = "\n\n".join([
        t("subscription.checkout_title", lang, price=product.format_amount()),
        t("subscription.checkout_open", lang),
    ])
    await cb.message.edit_text(
        text, reply_markup=checkout_keyboard(checkout.pay_url or "", lang))
