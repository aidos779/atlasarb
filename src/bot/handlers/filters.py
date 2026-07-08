"""Filter Panel handlers (PRD §12) — numeric steppers, multi-selects, save/reset,
tier gating with inline upsell (BR-FILTER-2), typed-value input (governed by /cancel)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.context import BotContext
from src.bot.handlers.common import render_signal_list
from src.bot.i18n import t
from src.bot.keyboards.screens import filters_panel, multiselect, numeric_editor
from src.bot.states.states import FilterStates
from src.domain.entitlements import entitlements_for
from src.domain.enums import ArbitrageType, Network
from src.domain.user import UserFilter, UserProfile

router = Router(name="filters")

_NUMERIC = {"min_profit", "liquidity", "signal_age", "risk"}
_STEP = {"min_profit": Decimal("0.1"), "liquidity": Decimal("500"),
         "signal_age": Decimal("15"), "risk": Decimal("1")}
_TIER_NAME = {"network": "Basic", "liquidity": "Basic", "arbitrage_type": "Basic",
              "risk": "Pro", "signal_age": "Pro"}


def _get_numeric(profile: UserProfile, field: str) -> Decimal:
    f = profile.filter
    return {"min_profit": f.min_profit_pct, "liquidity": f.min_liquidity_usd,
            "signal_age": Decimal(f.max_signal_age_sec),
            "risk": Decimal(f.max_risk_numeric)}[field]


def _set_numeric(profile: UserProfile, field: str, value: Decimal) -> None:
    f = profile.filter
    value = max(Decimal(0), value)
    if field == "min_profit":
        f.min_profit_pct = value
    elif field == "liquidity":
        f.min_liquidity_usd = value
    elif field == "signal_age":
        f.max_signal_age_sec = int(value)
    elif field == "risk":
        f.max_risk_numeric = max(1, min(5, int(value)))


@router.callback_query(F.data == "filters:open")
async def open_panel(cb: CallbackQuery, profile: UserProfile) -> None:
    lang = profile.settings.language.value
    ent = entitlements_for(profile.effective_tier)
    await cb.message.edit_text(t("filters.title", lang),
                               reply_markup=filters_panel(profile, ent, lang))
    await cb.answer()


@router.callback_query(F.data.startswith("filters:edit:"))
async def edit_filter(cb: CallbackQuery, profile: UserProfile) -> None:
    field = cb.data.split(":")[-1]
    lang = profile.settings.language.value
    ent = entitlements_for(profile.effective_tier)
    if not ent.filter_allowed(field):
        await cb.answer(t("filters.upsell", lang, filter=field.replace("_", " ").title(),
                          tier=_TIER_NAME.get(field, "Basic")), show_alert=True)
        return
    if field in _NUMERIC:
        await cb.message.edit_text(
            f"Edit {field.replace('_', ' ').title()}:",
            reply_markup=numeric_editor(field, str(_get_numeric(profile, field)), lang))
    else:
        options, selected = _multiselect_options(field, profile, ctx_engine=None)
        await cb.message.edit_text(f"Select {field.replace('_', ' ').title()}:",
                                   reply_markup=multiselect(field, options, selected, lang))
    await cb.answer()


def _multiselect_options(field: str, profile: UserProfile, ctx_engine):
    f = profile.filter
    if field == "network":
        return [n.display for n in Network], {n for n in f.networks}
    if field == "arbitrage_type":
        ent = entitlements_for(profile.effective_tier)
        return ([tp.value for tp in ent.allowed_arb_types],
                {tp.value for tp in f.arb_types})
    if field == "exchange":
        return (["binance", "bybit", "okx", "bitget", "mexc",
                 "uniswap_ethereum", "pancakeswap_bnb", "jupiter"], set(f.exchanges))
    # coin
    return (["BTC", "ETH", "SOL", "BNB", "ARB", "OP", "MATIC"], set(f.coins))


@router.callback_query(F.data.startswith("filters:step:"))
async def step_filter(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    _, _, field, direction = cb.data.split(":")
    step = _STEP[field] * (Decimal(1) if direction == "+" else Decimal(-1))
    _set_numeric(profile, field, _get_numeric(profile, field) + step)
    await ctx.users.save(profile)
    lang = profile.settings.language.value
    await cb.message.edit_reply_markup(
        reply_markup=numeric_editor(field, str(_get_numeric(profile, field)), lang))
    await cb.answer()


@router.callback_query(F.data.startswith("filters:type:"))
async def type_filter(cb: CallbackQuery, profile: UserProfile, state: FSMContext) -> None:
    field = cb.data.split(":")[-1]
    await state.set_state(FilterStates.awaiting_value)
    await state.update_data(field=field)
    await cb.message.answer(f"Type a value for {field.replace('_', ' ').title()} "
                            f"(or /cancel):")
    await cb.answer()


@router.message(FilterStates.awaiting_value, F.text)
async def typed_value(message: Message, ctx: BotContext, profile: UserProfile,
                      state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("field")
    await state.clear()
    try:
        value = Decimal(message.text.strip().replace("%", "").replace("$", "").replace(",", ""))
    except (InvalidOperation, AttributeError):
        await message.answer(t("error.generic", profile.settings.language.value))
        return
    _set_numeric(profile, field, value)
    await ctx.users.save(profile)
    await message.answer(t("settings.updated", profile.settings.language.value,
                           field=field.replace("_", " ").title(),
                           value=str(_get_numeric(profile, field))))


@router.callback_query(F.data.startswith("filters:toggle:"))
async def toggle_multiselect(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    _, _, field, value = cb.data.split(":", 3)
    f = profile.filter
    lang = profile.settings.language.value
    if field == "network":
        key = next((n.value for n in Network if n.display == value), value)
        f.networks = _toggle(f.networks, key)
    elif field == "arbitrage_type":
        current = {t.value for t in f.arb_types}
        toggled = _toggle(frozenset(current), value)
        f.arb_types = frozenset(ArbitrageType(v) for v in toggled)
    elif field == "exchange":
        # Free tier max 1 exchange (§12.3).
        ent = entitlements_for(profile.effective_tier)
        newset = _toggle(f.exchanges, value)
        if ent.tier.value == "free" and len(newset) > 1:
            await cb.answer(t("filters.upsell", lang, filter="Multiple Exchanges",
                              tier="Basic"), show_alert=True)
            return
        f.exchanges = newset
    else:  # coin
        ent = entitlements_for(profile.effective_tier)
        newset = _toggle(f.coins, value)
        if ent.tier.value == "free" and len(newset) > 3:
            await cb.answer(t("filters.upsell", lang, filter="More Coins", tier="Basic"),
                            show_alert=True)
            return
        f.coins = newset
    await ctx.users.save(profile)
    options, selected = _multiselect_options(field, profile, None)
    await cb.message.edit_reply_markup(
        reply_markup=multiselect(field, options, selected, lang))
    await cb.answer()


def _toggle(current: frozenset, value: str) -> frozenset:
    s = set(current)
    if value in s:
        s.discard(value)
    else:
        s.add(value)
    return frozenset(s)


@router.callback_query(F.data == "filters:save")
async def save_filters(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    await ctx.users.save(profile)
    await cb.answer(t("filters.saved", profile.settings.language.value))
    await render_signal_list(cb, ctx, profile)


@router.callback_query(F.data == "filters:reset")
async def reset_filters(cb: CallbackQuery, ctx: BotContext, profile: UserProfile) -> None:
    profile.filter = UserFilter()
    await ctx.users.save(profile)
    lang = profile.settings.language.value
    ent = entitlements_for(profile.effective_tier)
    await cb.message.edit_text(t("filters.reset", lang),
                               reply_markup=filters_panel(profile, ent, lang))
    await cb.answer()
