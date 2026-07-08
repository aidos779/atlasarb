"""Support handlers (PRD §8.2, §16.8) — FAQ + Contact Support ticket creation."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.context import BotContext
from src.bot.i18n import t
from src.bot.keyboards.inline import back_home
from src.bot.keyboards.screens import support_menu
from src.bot.states.states import SupportStates
from src.domain.user import UserProfile

router = Router(name="support")

_FAQ = (
    "📖 <b>FAQ</b>\n\n"
    "• We never ask for exchange API keys or funds — signals only.\n"
    "• Free tier: 60s delay, top 5 signals, CEX↔CEX only.\n"
    "• Upgrade for real-time, more filters, and history.\n"
    "• Signals are informational; you execute trades yourself."
)


@router.callback_query(F.data == "menu:support")
async def menu_support(cb: CallbackQuery, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    await cb.message.edit_text(t("support.title", lang), reply_markup=support_menu(lang))
    await cb.answer()


@router.callback_query(F.data == "support:faq")
async def faq(cb: CallbackQuery, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    await cb.message.edit_text(_FAQ, reply_markup=back_home(lang, back="menu:support"))
    await cb.answer()


@router.callback_query(F.data == "support:contact")
async def contact(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    lang = profile.settings.language.value
    await state.set_state(SupportStates.awaiting_message)
    await cb.message.answer(t("support.prompt", lang))
    await cb.answer()


@router.message(SupportStates.awaiting_message, F.text)
async def submit_ticket(message: Message, ctx: BotContext, profile: UserProfile,
                        state: FSMContext) -> None:
    await state.clear()
    await ctx.support.create_ticket(profile.telegram_user_id, message.text.strip())
    lang = profile.settings.language.value
    await message.answer(t("support.created", lang), reply_markup=back_home(lang))
