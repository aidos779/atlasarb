"""Search handlers (PRD §11) — coin/exchange search with temporary filter lens."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.callbacks import ack
from src.bot.context import BotContext
from src.bot.handlers.common import SESSIONS, render_signal_list
from src.bot.states.states import SearchStates
from src.domain.user import UserProfile
from src.i18n import t

router = Router(name="search")


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, ctx: BotContext,
                     profile: UserProfile, state: FSMContext) -> None:
    query = (command.args or "").strip()
    if not query:
        await state.set_state(SearchStates.awaiting_query)
        await message.answer(t("search.prompt", profile.settings.language.value))
        return
    await _do_search(message, ctx, profile, query)


@router.callback_query(F.data == "search:open")
async def search_open(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    await ack(cb)
    await state.set_state(SearchStates.awaiting_query)
    await cb.message.answer(t("search.prompt", profile.settings.language.value))


@router.message(SearchStates.awaiting_query, F.text)
async def search_input(message: Message, ctx: BotContext, profile: UserProfile,
                       state: FSMContext) -> None:
    await state.clear()
    await _do_search(message, ctx, profile, message.text.strip())


async def _do_search(message: Message, ctx: BotContext, profile: UserProfile,
                     query: str) -> None:
    lang = profile.settings.language.value
    if len(query) < 2:
        await message.answer(t("search.empty", lang))
        return
    result = ctx.search.search(profile.telegram_user_id, query)
    if result.rate_limited:
        await message.answer(t("search.too_fast", lang))
        return
    if not result.coins and not result.exchanges:
        await message.answer(t("search.none", lang, query=query))
        return
    lines = [t("search.results", lang, query=query), ""]
    b = InlineKeyboardBuilder()
    if result.coins:
        lines.append(f"<b>{t('search.coins', lang)}</b>")
        for sym, count in result.coins:
            lines.append(f"  ▸ {sym} — [{t('search.active_count', lang, count=count)}]")
            b.button(text=f"🪙 {sym}", callback_data=f"search:coin:{sym}")
    if result.exchanges:
        lines.append(f"<b>{t('search.exchanges', lang)}</b>")
        for name, count in result.exchanges:
            lines.append(f"  ▸ {name} — [{t('search.active_count', lang, count=count)}]")
            b.button(text=f"🏦 {name}", callback_data=f"search:exch:{name}")
    b.adjust(2)
    await message.answer("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("search:coin:"))
async def apply_coin_filter(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    session = SESSIONS.get(profile.telegram_user_id)
    session.search_coin = cb.data.split(":")[-1]
    session.page = 1
    await render_signal_list(cb, ctx, profile)


@router.callback_query(F.data.startswith("search:exch:"))
async def apply_exchange_filter(cb: CallbackQuery, ctx: BotContext,
                                profile: UserProfile) -> None:
    session = SESSIONS.get(profile.telegram_user_id)
    session.search_exchange = cb.data.split(":")[-1]
    session.page = 1
    await render_signal_list(cb, ctx, profile)


@router.callback_query(F.data == "search:clear")
async def clear_search(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    session = SESSIONS.get(profile.telegram_user_id)
    session.search_coin = None
    session.search_exchange = None
    await render_signal_list(cb, ctx, profile)
