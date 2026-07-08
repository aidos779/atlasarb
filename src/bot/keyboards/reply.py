"""Persistent reply keyboard (PRD §6.1) — 4 shortcuts shown at all times post-onboarding."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from src.bot.i18n import t


def main_reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t("menu.signals", lang)),
             KeyboardButton(text=t("menu.favorites", lang))],
            [KeyboardButton(text=t("menu.subscription", lang)),
             KeyboardButton(text=t("menu.settings", lang))],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
