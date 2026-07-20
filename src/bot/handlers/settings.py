"""Settings handlers (PRD §14) — language, timezone, currency; immediate + confirmed."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from src.bot.callbacks import ack
from src.bot.context import BotContext
from src.bot.keyboards.inline import currency_keyboard, language_keyboard, timezone_keyboard
from src.bot.keyboards.reply import main_reply_keyboard
from src.bot.keyboards.screens import settings_menu
from src.domain.enums import Currency, Language
from src.domain.user import UserProfile
from src.i18n import language_name, t, translations_of

router = Router(name="settings")


async def _show_settings(event, profile: UserProfile, need_ack: bool = True) -> None:
    lang = profile.settings.language.value
    kb = settings_menu(profile, lang)
    if isinstance(event, CallbackQuery):
        if need_ack:
            await ack(event)
        await event.message.edit_text(t("settings.title", lang), reply_markup=kb)
    else:
        await event.answer(t("settings.title", lang), reply_markup=kb)


@router.message(Command("settings"))
async def cmd_settings(message: Message, profile: UserProfile) -> None:
    await _show_settings(message, profile)


@router.message(F.text.in_(translations_of("menu.settings")))
async def reply_settings(message: Message, profile: UserProfile) -> None:
    await _show_settings(message, profile)


@router.callback_query(F.data == "menu:settings")
async def menu_settings(cb: CallbackQuery, profile: UserProfile) -> None:
    await _show_settings(cb, profile)


@router.callback_query(F.data == "settings:language")
async def choose_language(cb: CallbackQuery, profile: UserProfile) -> None:
    await ack(cb)
    await cb.message.edit_text(t("onboarding.language", profile.settings.language.value),
                               reply_markup=language_keyboard("set:lang"))


@router.callback_query(F.data == "settings:timezone")
async def choose_timezone(cb: CallbackQuery, profile: UserProfile) -> None:
    await ack(cb)
    await cb.message.edit_text(t("onboarding.timezone", profile.settings.language.value),
                               reply_markup=timezone_keyboard("set:tz"))


@router.callback_query(F.data == "settings:currency")
async def choose_currency(cb: CallbackQuery, profile: UserProfile) -> None:
    await ack(cb)
    await cb.message.edit_text(t("onboarding.currency", profile.settings.language.value),
                               reply_markup=currency_keyboard("set:cur"))


@router.callback_query(F.data.startswith("set:lang:"))
async def set_language(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    code = cb.data.split(":")[-1]
    await ack(cb)
    await ctx.users.set_language(profile, Language(code))
    profile.settings.language = Language(code)
    # Everything below re-renders in the NEW language: the confirmation, the persistent
    # reply keyboard, and the settings screen itself — no restart, no stale menu.
    await cb.message.answer(
        t("settings.updated", code, field=t("settings.field.language", code),
          value=language_name(code)),
        reply_markup=main_reply_keyboard(code))
    await _show_settings(cb, profile, need_ack=False)


@router.callback_query(F.data.startswith("set:tz:"))
async def set_timezone(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    tz = cb.data.split(":", 2)[-1]
    lang = profile.settings.language.value
    # Toast first — its content is known before any I/O.
    await ack(cb, t("settings.updated", lang, field=t("settings.field.timezone", lang),
                    value=tz))
    await ctx.users.set_timezone(profile, tz)
    profile.settings.timezone = tz
    await _show_settings(cb, profile, need_ack=False)


@router.callback_query(F.data.startswith("set:cur:"))
async def set_currency(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    cur = cb.data.split(":")[-1]
    lang = profile.settings.language.value
    await ack(cb, t("settings.updated", lang, field=t("settings.field.currency", lang),
                    value=cur))
    await ctx.users.set_currency(profile, Currency(cur))
    profile.settings.currency = Currency(cur)
    await _show_settings(cb, profile, need_ack=False)
