"""Shared handler helpers: per-user UI session state + render helpers for the Main Menu
and Signal List (PRD §8, §9). Message-editing over message-spam (§6.1) where possible.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from src.bot.context import BotContext
from src.bot.formatters.signal import format_card
from src.bot.i18n import t
from src.bot.keyboards.inline import main_menu, signal_list_controls
from src.domain.entitlements import UNLIMITED, entitlements_for
from src.domain.user import UserFilter, UserProfile

_SORT_MODES = ["profit", "spread", "liquidity", "risk", "age"]
_PAGE_SIZE = 5


@dataclass
class UiSession:
    page: int = 1
    sort_idx: int = 0
    descending: bool = True
    hidden: set[str] = field(default_factory=set)
    search_coin: str | None = None
    search_exchange: str | None = None
    last_refresh: float = 0.0

    @property
    def sort(self) -> str:
        return _SORT_MODES[self.sort_idx % len(_SORT_MODES)]

    def cycle_sort(self) -> None:
        self.sort_idx = (self.sort_idx + 1) % len(_SORT_MODES)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[int, UiSession] = {}

    def get(self, user_id: int) -> UiSession:
        return self._sessions.setdefault(user_id, UiSession())


SESSIONS = SessionStore()


def effective_filter(profile: UserProfile, session: UiSession) -> UserFilter:
    """Apply temporary search lens over saved filter (BR-SEARCH-3) without persisting."""
    f = profile.filter
    if not session.search_coin and not session.search_exchange:
        return f
    coins = {session.search_coin} if session.search_coin else f.coins
    exchanges = {session.search_exchange} if session.search_exchange else f.exchanges
    return UserFilter(
        min_profit_pct=f.min_profit_pct, coins=frozenset(coins),
        exchanges=frozenset(exchanges), networks=f.networks,
        min_liquidity_usd=f.min_liquidity_usd, max_risk_numeric=f.max_risk_numeric,
        max_signal_age_sec=f.max_signal_age_sec, arb_types=f.arb_types,
        scan_all_assets=f.scan_all_assets,
    )


async def render_main_menu(event: Message | CallbackQuery, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    text = t("menu.title", lang)
    kb = main_menu(lang)
    await _render(event, text, kb)


async def render_signal_list(event: Message | CallbackQuery, ctx: BotContext,
                             profile: UserProfile) -> None:
    lang = profile.settings.language.value
    session = SESSIONS.get(profile.telegram_user_id)
    ent = entitlements_for(profile.effective_tier)
    delay = ent.signal_delay_sec

    signals = ctx.registry.query(
        effective_filter(profile, session), sort=session.sort,
        descending=session.descending, allowed_types=ent.allowed_arb_types, delay_sec=delay,
    )
    signals = [s for s in signals if s.id not in session.hidden]
    if ent.signals_per_refresh != UNLIMITED:
        signals = signals[: ent.signals_per_refresh]

    if not signals:
        await _render(event, t("signals.empty", lang), main_menu(lang))
        return

    total_pages = max(1, (len(signals) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    session.page = min(session.page, total_pages)
    start = (session.page - 1) * _PAGE_SIZE
    page_signals = signals[start:start + _PAGE_SIZE]

    fav_ids = set(await ctx.favorites.values(profile.telegram_user_id, "signal"))
    cards_text = []
    cards_meta = []
    for s in page_signals:
        cards_text.append(await format_card(
            s, profile, ctx.fx, ctx.config_manager.config.confidence_threshold))
        cards_meta.append((s, s.id in fav_ids))

    header = t("signals.page", lang, current=session.page, total=total_pages)
    if profile.effective_tier.value == "free":
        header = t("signals.free_notice", lang) + "\n\n" + header
    text = header + "\n\n" + "\n\n".join(cards_text)
    kb = signal_list_controls(lang, session.sort, session.page, total_pages, cards_meta)
    await _render(event, text, kb)


async def _render(event: Message | CallbackQuery, text: str, kb) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest:
            await event.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, disable_web_page_preview=True)


def is_onboarded(profile: UserProfile) -> bool:
    return profile.onboarding_complete
