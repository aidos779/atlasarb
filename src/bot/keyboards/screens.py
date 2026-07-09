"""Screen-specific inline keyboards: filters, settings, subscription, favorites,
notifications, support, admin (PRD §12, §14, §15, §8, §13, §16)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.i18n import t
from src.domain.entitlements import Entitlements, TierPricing
from src.domain.user import UserProfile


def _nav(b: InlineKeyboardBuilder, lang: str, back: str = "nav:home") -> None:
    b.row(InlineKeyboardButton(text=t("nav.back", lang), callback_data=back),
          InlineKeyboardButton(text=t("nav.home", lang), callback_data="nav:home"))


def filters_panel(profile: UserProfile, ent: Entitlements, lang: str) -> InlineKeyboardMarkup:
    f = profile.filter
    b = InlineKeyboardBuilder()
    rows = [
        ("min_profit", f"Minimum Profit: {f.min_profit_pct}%"),
        ("coin", f"Coin: {', '.join(sorted(f.coins)) or 'Any'}"),
        ("exchange", f"Exchange: {', '.join(sorted(f.exchanges)) or 'Any'}"),
        ("network", f"Network: {', '.join(sorted(f.networks)) or 'Any'}"),
        ("liquidity", f"Liquidity (min): ${f.min_liquidity_usd:g}"),
        ("risk", f"Risk: ≤ {f.max_risk_numeric}"),
        ("signal_age", f"Signal Age (max): {f.max_signal_age_sec}s"),
        ("arbitrage_type", f"Arb Type: {', '.join(sorted(t.value for t in f.arb_types)) or 'All'}"),
    ]
    for field, label in rows:
        lock = "" if ent.filter_allowed(field) else " 🔒"
        b.button(text=f"{label}{lock}", callback_data=f"filters:edit:{field}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="💾 Save as Default", callback_data="filters:save"),
          InlineKeyboardButton(text="♻️ Reset", callback_data="filters:reset"))
    _nav(b, lang, back="menu:signals")
    return b.as_markup()


def numeric_editor(field: str, value: str, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="−", callback_data=f"filters:step:{field}:-")
    b.button(text=f"{value}", callback_data="noop")
    b.button(text="+", callback_data=f"filters:step:{field}:+")
    b.adjust(3)
    b.button(text="✏️ Type value", callback_data=f"filters:type:{field}")
    b.button(text="✅ Done", callback_data="filters:open")
    b.adjust(3, 2)
    return b.as_markup()


def multiselect(field: str, options: list[str], selected: set[str],
                lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for opt in options:
        mark = "✅" if opt in selected else "⬜"
        b.button(text=f"{mark} {opt}", callback_data=f"filters:toggle:{field}:{opt}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="✅ Done", callback_data="filters:open"))
    return b.as_markup()


def settings_menu(profile: UserProfile, lang: str) -> InlineKeyboardMarkup:
    s = profile.settings
    b = InlineKeyboardBuilder()
    b.button(text=f"🌐 Language: {s.language.value}", callback_data="settings:language")
    b.button(text=f"🕒 Timezone: {s.timezone}", callback_data="settings:timezone")
    b.button(text=f"💵 Currency: {s.currency.value}", callback_data="settings:currency")
    b.button(text=f"📈 Minimum Profit: {profile.filter.min_profit_pct}%",
             callback_data="filters:edit:min_profit")
    b.button(text="🏦 Favorite Exchanges", callback_data="fav:exchange")
    b.button(text="🪙 Favorite Coins", callback_data="fav:coin")
    b.button(text="🔔 Notification Preferences", callback_data="menu:notifications")
    b.adjust(1)
    _nav(b, lang)
    return b.as_markup()


def subscription_menu(profile: UserProfile, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Plan Comparison", callback_data="sub:compare")
    if profile.effective_tier.value != "pro":
        b.button(text="⬆️ Upgrade", callback_data="sub:compare")
    if profile.subscription.is_paid_active:
        b.button(text=t("subscription.cancel_btn", lang), callback_data="sub:cancel")
    b.adjust(1)
    _nav(b, lang)
    return b.as_markup()


def plan_comparison(pricing: list[TierPricing], lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for plan in pricing:
        if plan.tier.value == "free":
            continue
        b.button(text=f"Choose {plan.tier.value.title()} — ${plan.monthly_usd:g}/mo",
                 callback_data=f"sub:choose:{plan.tier.value}")
    b.adjust(1)
    _nav(b, lang, back="menu:subscription")
    return b.as_markup()


def checkout_keyboard(tier: str, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t("subscription.confirm", lang), callback_data=f"sub:confirm:{tier}")
    b.button(text=t("nav.back", lang), callback_data="sub:compare")
    b.adjust(1)
    return b.as_markup()


def retry_payment(tier: str, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t("subscription.try_again", lang), callback_data=f"sub:confirm:{tier}")
    b.row(InlineKeyboardButton(text=t("nav.home", lang), callback_data="nav:home"))
    return b.as_markup()


def favorites_hub(lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t("favorites.signals", lang), callback_data="fav:signal")
    b.button(text=t("favorites.coins", lang), callback_data="fav:coin")
    b.button(text=t("favorites.exchanges", lang), callback_data="fav:exchange")
    b.adjust(1)
    _nav(b, lang)
    return b.as_markup()


def notifications_menu(profile: UserProfile, ent: Entitlements, lang: str) -> InlineKeyboardMarkup:
    s = profile.settings
    b = InlineKeyboardBuilder()
    instant_icon = '🔔' if s.instant_alerts_enabled else '🔕'
    b.button(text=f"{instant_icon} {t('notifications.instant', lang)}",
             callback_data="notif:toggle:instant")
    b.button(text=f"{'✅' if s.daily_summary_enabled else '⬜'} {t('notifications.daily', lang)}",
             callback_data="notif:toggle:daily")
    fav_lock = "" if ent.favorite_entity_alerts else " 🔒"
    coin_icon = '✅' if s.favorite_coin_alerts else '⬜'
    b.button(text=f"{coin_icon} {t('notifications.fav_coin', lang)}{fav_lock}",
             callback_data="notif:toggle:fav_coin")
    exch_icon = '✅' if s.favorite_exchange_alerts else '⬜'
    b.button(text=f"{exch_icon} {t('notifications.fav_exchange', lang)}{fav_lock}",
             callback_data="notif:toggle:fav_exchange")
    b.button(text=f"🔒 {t('notifications.subscription', lang)}", callback_data="noop")
    b.button(text=f"🔒 {t('notifications.system', lang)}", callback_data="noop")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🔕 1h", callback_data="notif:pause:1"),
          InlineKeyboardButton(text="🔕 4h", callback_data="notif:pause:4"),
          InlineKeyboardButton(text="🔕 Tomorrow", callback_data="notif:pause:24"))
    _nav(b, lang)
    return b.as_markup()


def support_menu(lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t("support.faq", lang), callback_data="support:faq")
    b.button(text=t("support.contact", lang), callback_data="support:contact")
    b.adjust(1)
    _nav(b, lang)
    return b.as_markup()


def admin_menu(lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👥 User Management", callback_data="admin:users")
    b.button(text="💳 Subscription Management", callback_data="admin:subs")
    b.button(text="📢 Broadcast Messages", callback_data="admin:broadcast")
    b.button(text="📊 Analytics", callback_data="admin:analytics")
    b.button(text="📡 Signal Monitoring", callback_data="admin:monitoring")
    b.button(text="📜 Logs", callback_data="admin:logs")
    b.button(text="🆘 Support Queue", callback_data="admin:support")
    b.adjust(1)
    b.row(InlineKeyboardButton(text=t("nav.home", lang), callback_data="nav:home"))
    return b.as_markup()
