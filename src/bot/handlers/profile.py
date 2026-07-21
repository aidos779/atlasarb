"""Profile, History, Analytics handlers (PRD §7.2 /profile /history, §8.2 Analytics)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.callbacks import ack
from src.bot.context import BotContext
from src.bot.formatters.money import format_datetime
from src.bot.keyboards.inline import back_home
from src.domain.user import UserProfile
from src.i18n import t, tier_label

router = Router(name="profile")


@router.message(Command("profile"))
async def cmd_profile(message: Message, ctx: BotContext, profile: UserProfile) -> None:
    await _render_profile(message, ctx, profile)


@router.callback_query(F.data == "menu:profile")
async def menu_profile(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    await _render_profile(cb, ctx, profile)


async def _render_profile(event, ctx: BotContext, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    since = (format_datetime(profile.created_at, profile.settings.timezone)
             if profile.created_at else t("profile.unknown", lang))
    lines = [
        t("profile.title", lang), "",
        f"{profile.first_name or ''} @{profile.username or t('profile.unknown', lang)}",
        f"{t('profile.member_since', lang)}: {since}",
        f"{t('profile.plan', lang)}: {tier_label(profile.effective_tier, lang)}",
    ]

    b = InlineKeyboardBuilder()
    if profile.subscription.is_pro:
        lines.append(f"{t('profile.signals', lang)}: {t('profile.signals_unlimited', lang)}")
        lines.append(f"{t('profile.payment', lang)}: {t('profile.payment_completed', lang)}")
    else:
        allowance = await ctx.signal_access.allowance(profile)
        if allowance.unlimited:
            # Staff / dev builds resolve to unlimited without a purchase.
            lines.append(
                f"{t('profile.signals', lang)}: {t('profile.signals_unlimited', lang)}")
        else:
            lines.append(t("profile.signals_used", lang, used=allowance.delivered,
                           quota=allowance.quota))
            lines.append(t("profile.remaining", lang, remaining=allowance.remaining))
            b.button(text=t("btn.upgrade", lang), callback_data="sub:buy")

    b.row(*back_home(lang).inline_keyboard[0])
    kb = b.as_markup()
    if isinstance(event, CallbackQuery):
        await ack(event)
        await event.message.edit_text("\n".join(lines), reply_markup=kb)
    else:
        await event.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data == "menu:analytics")
async def menu_analytics(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    await ack(cb)
    lang = profile.settings.language.value
    summary = ctx.analytics.today()
    none = t("analytics.none", lang)
    coins = ", ".join(f"{c}({n})" for c, n in summary.top_coins) or none
    exch = ", ".join(f"{e}({n})" for e, n in summary.top_exchanges) or none
    text = "\n".join([
        f"<b>{t('analytics.title', lang)}</b>", "",
        f"{t('analytics.signals_today', lang)}: {summary.total_today}",
        f"{t('analytics.active_now', lang)}: {summary.total_active}",
        f"{t('analytics.avg_profit', lang)}: {summary.avg_profit_pct:.2f}%",
        f"{t('analytics.top_coins', lang)}: {coins}",
        f"{t('analytics.top_exchanges', lang)}: {exch}",
    ])
    await cb.message.edit_text(text, reply_markup=back_home(lang))


@router.message(Command("history"))
async def cmd_history(message: Message, ctx: BotContext, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    viewed = await ctx.history.viewed(profile.telegram_user_id)
    expired = await ctx.history.expired(profile.telegram_user_id)
    fav = await ctx.history.favorites_history(profile.telegram_user_id)

    def fmt(rows) -> str:
        if not rows:
            return t("history.empty", lang)
        return "\n".join(
            f"• {r.coin}/{r.trading_pair.split('/')[-1]} {r.net_profit_pct:.2f}% "
            f"({r.buy_exchange}→{r.sell_exchange})" for r in rows[:10])

    text = (
        f"<b>{t('history.viewed', lang)}</b>\n{fmt(viewed)}\n\n"
        f"<b>{t('history.expired', lang)}</b>\n{fmt(expired)}\n\n"
        f"<b>{t('history.fav', lang)}</b>\n{fmt(fav)}"
    )
    await message.answer(text, reply_markup=back_home(lang))
