"""Screen-specific inline keyboards: filters, settings, subscription, favorites,
notifications, support, admin (PRD §12, §14, §15, §8, §13, §16)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.domain.entitlements import Entitlements, TierPricing
from src.domain.user import UserProfile
from src.i18n import filter_label, language_name, t


def _nav(b: InlineKeyboardBuilder, lang: str, back: str = "nav:home") -> None:
    b.row(InlineKeyboardButton(text=t("nav.back", lang), callback_data=back),
          InlineKeyboardButton(text=t("nav.home", lang), callback_data="nav:home"))


def filters_panel(profile: UserProfile, ent: Entitlements, lang: str) -> InlineKeyboardMarkup:
    f = profile.filter
    b = InlineKeyboardBuilder()
    any_ = t("filters.any", lang)
    rows = [
        ("min_profit", f"{f.min_profit_pct}%"),
        ("coin", ", ".join(sorted(f.coins)) or any_),
        ("exchange", ", ".join(sorted(f.exchanges)) or any_),
        ("network", ", ".join(sorted(f.networks)) or any_),
        ("liquidity", f"${f.min_liquidity_usd:g}"),
        ("risk", f"≤ {f.max_risk_numeric}"),
        ("signal_age", f"{f.max_signal_age_sec}s"),
        ("arbitrage_type",
         ", ".join(sorted(a.value for a in f.arb_types)) or t("filters.all", lang)),
    ]
    for field, value in rows:
        lock = "" if ent.filter_allowed(field) else " 🔒"
        b.button(text=f"{filter_label(field, lang)}: {value}{lock}",
                 callback_data=f"filters:edit:{field}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text=t("btn.save_default", lang), callback_data="filters:save"),
          InlineKeyboardButton(text=t("btn.reset", lang), callback_data="filters:reset"))
    _nav(b, lang, back="menu:signals")
    return b.as_markup()


def numeric_editor(field: str, value: str, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="−", callback_data=f"filters:step:{field}:-")
    b.button(text=f"{value}", callback_data="noop")
    b.button(text="+", callback_data=f"filters:step:{field}:+")
    b.adjust(3)
    b.button(text=t("btn.type_value", lang), callback_data=f"filters:type:{field}")
    b.button(text=t("btn.done", lang), callback_data="filters:open")
    b.adjust(3, 2)
    return b.as_markup()


def multiselect(field: str, options: list[str], selected: set[str],
                lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for opt in options:
        mark = "✅" if opt in selected else "⬜"
        b.button(text=f"{mark} {opt}", callback_data=f"filters:toggle:{field}:{opt}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text=t("btn.done", lang), callback_data="filters:open"))
    return b.as_markup()


def settings_menu(profile: UserProfile, lang: str) -> InlineKeyboardMarkup:
    s = profile.settings
    b = InlineKeyboardBuilder()
    b.button(text=t("settings.language", lang, value=language_name(s.language.value)),
             callback_data="settings:language")
    b.button(text=t("settings.timezone", lang, value=s.timezone),
             callback_data="settings:timezone")
    b.button(text=t("settings.currency", lang, value=s.currency.value),
             callback_data="settings:currency")
    b.button(text=t("settings.min_profit", lang, value=profile.filter.min_profit_pct),
             callback_data="filters:edit:min_profit")
    b.button(text=t("settings.fav_exchanges", lang), callback_data="fav:exchange")
    b.button(text=t("settings.fav_coins", lang), callback_data="fav:coin")
    b.button(text=t("settings.notif_prefs", lang), callback_data="menu:notifications")
    b.adjust(1)
    _nav(b, lang)
    return b.as_markup()


def subscription_menu(profile: UserProfile, lang: str, price_usd: float) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t("subscription.compare_btn", lang), callback_data="sub:compare")
    # Pro is a one-time purchase, so the only action a Pro user has left is looking at
    # the plans — nothing to upgrade to, nothing to cancel.
    if not profile.subscription.is_pro:
        b.button(text=t("subscription.buy_btn", lang, price=f"{price_usd:g}"),
                 callback_data="sub:buy")
    b.adjust(1)
    _nav(b, lang)
    return b.as_markup()


def plan_comparison(pricing: list[TierPricing], lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for plan in pricing:
        if plan.tier.value == "free":
            continue
        b.button(text=t("subscription.buy_btn", lang, price=f"{plan.price_usd:g}"),
                 callback_data="sub:buy")
    b.adjust(1)
    _nav(b, lang, back="menu:subscription")
    return b.as_markup()


def checkout_keyboard(pay_url: str, lang: str) -> InlineKeyboardMarkup:
    """Live checkout — a URL button straight to the provider's hosted invoice."""
    b = InlineKeyboardBuilder()
    b.button(text=t("subscription.pay_btn", lang), url=pay_url)
    b.adjust(1)
    _nav(b, lang, back="menu:subscription")
    return b.as_markup()


def paywall_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Paywall CTA. Deliberately keeps the full nav row: the quota closes the signal
    feed, not the rest of the bot."""
    b = InlineKeyboardBuilder()
    b.button(text=t("paywall.buy_btn", lang), callback_data="sub:buy")
    b.button(text=t("subscription.compare_btn", lang), callback_data="sub:compare")
    b.adjust(1)
    _nav(b, lang)
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
    b.row(InlineKeyboardButton(text=t("notifications.pause_hours", lang, hours=1),
                               callback_data="notif:pause:1"),
          InlineKeyboardButton(text=t("notifications.pause_hours", lang, hours=4),
                               callback_data="notif:pause:4"),
          InlineKeyboardButton(text=t("notifications.pause_tomorrow", lang),
                               callback_data="notif:pause:24"))
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
    b.button(text=t("admin.users_btn", lang), callback_data="admin:users")
    b.button(text=t("admin.subs_btn", lang), callback_data="admin:subs")
    b.button(text=t("admin.broadcast_btn", lang), callback_data="admin:broadcast")
    b.button(text=t("admin.analytics_btn", lang), callback_data="admin:analytics")
    b.button(text=t("admin.monitoring_btn", lang), callback_data="admin:monitoring")
    b.button(text=t("admin.logs_btn", lang), callback_data="admin:logs")
    b.button(text=t("admin.support_btn", lang), callback_data="admin:support")
    b.adjust(1)
    b.row(InlineKeyboardButton(text=t("nav.home", lang), callback_data="nav:home"))
    return b.as_markup()
