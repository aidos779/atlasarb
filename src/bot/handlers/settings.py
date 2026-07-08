"""Settings handlers (PRD §14) — language, timezone, currency; immediate + confirmed."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from src.bot.context import BotContext
from src.bot.i18n import t
from src.bot.keyboards.inline import currency_keyboard, language_keyboard, timezone_keyboard
from src.bot.keyboards.reply import main_reply_keyboard
from src.bot.keyboards.screens import settings_menu
from src.domain.enums import Currency, Language
from src.domain.user import UserProfile

router = Router(name="settings")


async def _show_settings(event, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    kb = settings_menu(profile, lang)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(t("settings.title", lang), reply_markup=kb)
        await event.answer()
    else:
        await event.answer(t("settings.title", lang), reply_markup=kb)


@router.message(Command("settings"))
async def cmd_settings(message: Message, profile: UserProfile) -> None:
    await _show_settings(message, profile)


@router.message(F.text.in_({"⚙️ Settings", "⚙️ Настройки", "⚙️ Баптаулар"}))
async def reply_settings(message: Message, profile: UserProfile) -> None:
    await _show_settings(message, profile)


@router.callback_query(F.data == "menu:settings")
async def menu_settings(cb: CallbackQuery, profile: UserProfile) -> None:
    await _show_settings(cb, profile)


@router.callback_query(F.data == "settings:language")
async def choose_language(cb: CallbackQuery, profile: UserProfile) -> None:
    await cb.message.edit_text(t("onboarding.language", profile.settings.language.value),
                               reply_markup=language_keyboard("set:lang"))
    await cb.answer()


@router.callback_query(F.data == "settings:timezone")
async def choose_timezone(cb: CallbackQuery, profile: UserProfile) -> None:
    await cb.message.edit_text(t("onboarding.timezone", profile.settings.language.value),
                               reply_markup=timezone_keyboard("set:tz"))
    await cb.answer()


@router.callback_query(F.data == "settings:currency")
async def choose_currency(cb: CallbackQuery, profile: UserProfile) -> None:
    await cb.message.edit_text(t("onboarding.currency", profile.settings.language.value),
                               reply_markup=currency_keyboard("set:cur"))
    await cb.answer()


@router.callback_query(F.data.startswith("set:lang:"))
async def set_language(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    code = cb.data.split(":")[-1]
    await ctx.users.set_language(profile, Language(code))
    profile.settings.language = Language(code)
    await cb.message.answer(
        t("settings.updated", code, field="Language", value=code),
        reply_markup=main_reply_keyboard(code))
    await _show_settings(cb, profile)


@router.callback_query(F.data.startswith("set:tz:"))
async def set_timezone(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    tz = cb.data.split(":", 2)[-1]
    await ctx.users.set_timezone(profile, tz)
    profile.settings.timezone = tz
    lang = profile.settings.language.value
    await cb.answer(t("settings.updated", lang, field="Timezone", value=tz))
    await _show_settings(cb, profile)


@router.callback_query(F.data.startswith("set:cur:"))
async def set_currency(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    cur = cb.data.split(":")[-1]
    await ctx.users.set_currency(profile, Currency(cur))
    profile.settings.currency = Currency(cur)
    lang = profile.settings.language.value
    await cb.answer(t("settings.updated", lang, field="Currency", value=cur))
    await _show_settings(cb, profile)
