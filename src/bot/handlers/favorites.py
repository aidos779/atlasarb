"""Favorites hub handlers (PRD §14.3, §8.2) — signals/coins/exchanges lists."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from src.bot.context import BotContext
from src.bot.keyboards.inline import back_home
from src.bot.keyboards.screens import favorites_hub
from src.domain.user import UserProfile
from src.i18n import t, translations_of

router = Router(name="favorites")

_KIND_TITLE = {"coin": "favorites.coins", "exchange": "favorites.exchanges",
               "signal": "favorites.signals"}


async def _show_hub(event, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    kb = favorites_hub(lang)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(t("favorites.title", lang), reply_markup=kb)
        await event.answer()
    else:
        await event.answer(t("favorites.title", lang), reply_markup=kb)


@router.message(Command("favorites"))
async def cmd_favorites(message: Message, profile: UserProfile) -> None:
    await _show_hub(message, profile)


@router.message(F.text.in_(translations_of("menu.favorites")))
async def reply_favorites(message: Message, profile: UserProfile) -> None:
    await _show_hub(message, profile)


@router.callback_query(F.data == "menu:favorites")
async def menu_favorites(cb: CallbackQuery, profile: UserProfile) -> None:
    await _show_hub(cb, profile)


@router.callback_query(F.data.startswith("fav:") & ~F.data.startswith("fav:signal:"))
async def list_favorites(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    kind = cb.data.split(":")[1]
    if kind not in _KIND_TITLE:
        await cb.answer()
        return
    lang = profile.settings.language.value
    rows = await ctx.favorites.list(profile.telegram_user_id, kind)
    title = t(_KIND_TITLE[kind], lang)
    if not rows:
        body = f"{title}\n\n{t('favorites.empty', lang)}"
    else:
        lines = [f"{'🔒 ' if r.frozen else ''}{r.value}" for r in rows]
        body = f"{title}\n\n" + "\n".join(lines)
    await cb.message.edit_text(body, reply_markup=back_home(lang, back="menu:favorites"))
    await cb.answer()
