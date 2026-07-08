"""Inline keyboards for every screen (PRD §6.2, §8–§16).

Every keyboard except the Main Menu includes a Back button; every screen except Main
Menu and Signal Details includes a Home button (R-NAV-2). Callback data uses compact
`domain:action[:arg]` tokens parsed by handlers.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.i18n import t
from src.domain.signal import Signal


def _nav_row(lang: str, home: bool = True, back: str = "nav:home") -> list[InlineKeyboardButton]:
    row = [InlineKeyboardButton(text=t("nav.back", lang), callback_data=back)]
    if home:
        row.append(InlineKeyboardButton(text=t("nav.home", lang), callback_data="nav:home"))
    return row


def main_menu(lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t("menu.signals", lang), callback_data="menu:signals")
    b.button(text=t("menu.analytics", lang), callback_data="menu:analytics")
    b.button(text=t("menu.favorites", lang), callback_data="menu:favorites")
    b.button(text=t("menu.notifications", lang), callback_data="menu:notifications")
    b.button(text=t("menu.subscription", lang), callback_data="menu:subscription")
    b.button(text=t("menu.settings", lang), callback_data="menu:settings")
    b.button(text=t("menu.support", lang), callback_data="menu:support")
    b.button(text=t("menu.profile", lang), callback_data="menu:profile")
    b.adjust(2)
    return b.as_markup()


def language_keyboard(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, label in (("en", "🇬🇧 English"), ("ru", "🇷🇺 Русский"), ("kk", "🇰🇿 Қазақша")):
        b.button(text=label, callback_data=f"{prefix}:{code}")
    b.adjust(1)
    return b.as_markup()


def timezone_keyboard(prefix: str) -> InlineKeyboardMarkup:
    zones = [("UTC", "UTC+0"), ("Asia/Almaty", "UTC+5"), ("Europe/Moscow", "UTC+3"),
             ("Europe/London", "UTC+0"), ("America/New_York", "UTC−5"), ("Asia/Dubai", "UTC+4")]
    b = InlineKeyboardBuilder()
    for tz, label in zones:
        b.button(text=f"{label} · {tz}", callback_data=f"{prefix}:{tz}")
    b.adjust(1)
    return b.as_markup()


def currency_keyboard(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for cur in ("USD", "EUR", "KZT", "RUB", "USDT"):
        b.button(text=cur, callback_data=f"{prefix}:{cur}")
    b.adjust(3)
    return b.as_markup()


def signal_card_buttons(signal: Signal, is_favorite: bool, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t("btn.details", lang), callback_data=f"sig:details:{signal.id}")
    fav_key = "btn.unfavorite" if is_favorite else "btn.favorite"
    b.button(text=t(fav_key, lang), callback_data=f"sig:fav:{signal.id}")
    b.button(text=t("btn.hide", lang), callback_data=f"sig:hide:{signal.id}")
    b.adjust(3)
    return b.as_markup()


def signal_list_controls(lang: str, sort_mode: str, page: int, total_pages: int,
                         cards: list[tuple[Signal, bool]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # Per-card action rows.
    for signal, is_fav in cards:
        b.row(
            InlineKeyboardButton(text=f"🔍 {signal.coin}", callback_data=f"sig:details:{signal.id}"),
            InlineKeyboardButton(
                text=t("btn.unfavorite" if is_fav else "btn.favorite", lang),
                callback_data=f"sig:fav:{signal.id}"),
            InlineKeyboardButton(text=t("btn.hide", lang), callback_data=f"sig:hide:{signal.id}"),
        )
    b.row(
        InlineKeyboardButton(text=t("btn.filters", lang), callback_data="filters:open"),
        InlineKeyboardButton(text=t("btn.search", lang), callback_data="search:open"),
    )
    b.row(
        InlineKeyboardButton(text=t("btn.sort", lang, mode=sort_mode), callback_data="sig:sort"),
        InlineKeyboardButton(text=t("btn.refresh", lang), callback_data="sig:refresh"),
    )
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text=t("btn.prev", lang),
                                            callback_data=f"sig:page:{page - 1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text=t("btn.next", lang),
                                            callback_data=f"sig:page:{page + 1}"))
        if nav:
            b.row(*nav)
    b.row(InlineKeyboardButton(text=t("nav.home", lang), callback_data="nav:home"))
    return b.as_markup()


def details_buttons(signal: Signal, is_favorite: bool, bot_username: str,
                    lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t("btn.unfavorite" if is_favorite else "btn.favorite", lang),
             callback_data=f"sig:fav:{signal.id}")
    b.button(text=t("btn.share", lang),
             url=f"https://t.me/{bot_username}?start=signal_{signal.id}")
    b.button(text=t("btn.report", lang), callback_data=f"sig:report:{signal.id}")
    b.button(text=t("btn.back_to_list", lang), callback_data="menu:signals")
    b.adjust(2)
    return b.as_markup()


def upsell_keyboard(lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Upgrade", callback_data="menu:subscription")
    b.row(*_nav_row(lang))
    return b.as_markup()


def back_home(lang: str, back: str = "nav:home") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(*_nav_row(lang, back=back))
    return b.as_markup()
