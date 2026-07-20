"""Entry-point handlers: /start (onboarding + deep links), /help, /menu, /cancel,
onboarding callbacks, and the global nav:home callback (PRD §7.2, §21.1, §21.5)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.callbacks import ack
from src.bot.context import BotContext
from src.bot.handlers.common import render_main_menu, render_signal_list
from src.bot.keyboards.inline import (
    currency_keyboard,
    language_keyboard,
    timezone_keyboard,
)
from src.bot.keyboards.reply import main_reply_keyboard
from src.domain.enums import Currency, Language
from src.domain.user import UserProfile
from src.i18n import t

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, ctx: BotContext,
                    profile: UserProfile, state: FSMContext) -> None:
    await state.clear()
    payload = (command.args or "").strip()
    if not profile.onboarding_complete:
        if payload:
            await ctx.users.set_pending_deeplink(profile, payload)  # BR-START-3
        await _resume_onboarding(message, profile)
        return
    if payload:
        await _apply_deeplink(message, ctx, profile, payload)
        return
    await _show_menu(message, profile)


async def _resume_onboarding(event: Message, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    step = profile.onboarding_step
    if step == 0:
        await event.answer(t("onboarding.welcome", lang))
        await event.answer(t("onboarding.language", lang),
                           reply_markup=language_keyboard("ob:lang"))
    elif step == 1:
        await event.answer(t("onboarding.timezone", lang),
                           reply_markup=timezone_keyboard("ob:tz"))
    elif step == 2:
        await event.answer(t("onboarding.currency", lang),
                           reply_markup=currency_keyboard("ob:cur"))


@router.callback_query(F.data.startswith("ob:lang:"))
async def onboarding_language(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    code = cb.data.split(":")[-1]
    await ack(cb)
    await ctx.users.set_language(profile, Language(code))
    lang = code
    await cb.message.edit_text(t("onboarding.timezone", lang),
                               reply_markup=timezone_keyboard("ob:tz"))


@router.callback_query(F.data.startswith("ob:tz:"))
async def onboarding_timezone(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    tz = cb.data.split(":", 2)[-1]
    await ack(cb)
    await ctx.users.set_timezone(profile, tz)
    lang = profile.settings.language.value
    await cb.message.edit_text(t("onboarding.currency", lang),
                               reply_markup=currency_keyboard("ob:cur"))


@router.callback_query(F.data.startswith("ob:cur:"))
async def onboarding_currency(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    cur = cb.data.split(":")[-1]
    await ack(cb)
    await ctx.users.set_currency(profile, Currency(cur))
    lang = profile.settings.language.value
    await cb.message.answer(t("onboarding.done", lang),
                            reply_markup=main_reply_keyboard(lang))
    # Apply any pending deep link captured pre-onboarding (BR-START-3).
    fresh = await ctx.users.get(profile.telegram_user_id)
    if fresh and fresh.pending_deeplink:
        payload = fresh.pending_deeplink
        await ctx.users.set_pending_deeplink(fresh, None)
        await _apply_deeplink(cb.message, ctx, fresh, payload)
    else:
        await render_main_menu(cb, fresh or profile, need_ack=False)


async def _apply_deeplink(event: Message, ctx: BotContext, profile: UserProfile,
                          payload: str) -> None:
    lang = profile.settings.language.value
    if payload.startswith("signal_"):
        signal_id = payload[len("signal_"):]
        signal = ctx.registry.get(signal_id)
        if signal is None:
            await event.answer(t("details.not_found", lang))
            await _show_menu(event, profile)
            return
        from src.bot.handlers.signals import send_details
        await send_details(event, ctx, profile, signal)
        return
    if payload.startswith("ref_"):
        # Attribution stored silently.
        await _show_menu(event, profile)
        return
    await _show_menu(event, profile)  # unknown payload ignored (logged upstream)


async def _show_menu(event: Message, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    await event.answer(t("menu.title", lang),
                       reply_markup=main_reply_keyboard(lang))
    from src.bot.keyboards.inline import main_menu
    await event.answer(t("menu.prompt", lang), reply_markup=main_menu(lang))


@router.message(Command("help"))
async def cmd_help(message: Message, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    from src.bot.keyboards.inline import back_home
    await message.answer(t("help.text", lang), reply_markup=back_home(lang))


@router.message(Command("menu"))
async def cmd_menu(message: Message, ctx: BotContext, profile: UserProfile,
                   state: FSMContext) -> None:
    await state.clear()  # BR-MENU-1 silently cancels pending input
    if not profile.onboarding_complete:
        await message.answer(t("onboarding.finish_first", profile.settings.language.value))
        await _resume_onboarding(message, profile)
        return
    await render_main_menu(message, profile)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, profile: UserProfile, state: FSMContext) -> None:
    await state.clear()
    await message.answer(t("cancel.done", profile.settings.language.value))


@router.callback_query(F.data == "nav:home")
async def nav_home(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    await state.clear()
    await render_main_menu(cb, profile)


@router.callback_query(F.data == "menu:signals")
async def menu_signals(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    await render_signal_list(cb, ctx, profile)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await ack(cb)
