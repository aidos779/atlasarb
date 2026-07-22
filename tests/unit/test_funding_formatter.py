"""Phase 1 — funding signals get a dedicated formatter, not the generic spot template.

The generic path rendered a delta-neutral funding carry as "Buy on X @ $1.00 / Sell on Y
@ $1.00" and a "Move coin → other exchange" trade route (the legs carry a $1 placeholder
price and no network). These assert the funding-specific alert, overview and execution
guide, and that neither the fake $1.00 price nor a transfer instruction survives.
"""
from __future__ import annotations

import time
from decimal import Decimal

import pytest

from src.bot.formatters.signal import _trade_route, format_alert, format_details
from src.domain.enums import ArbitrageType, Language, RiskScore
from src.domain.signal import ProfitBreakdown, Signal, SizingProfile
from src.domain.user import UserProfile
from src.i18n import LANGUAGES


class _Fx:
    async def convert(self, amount, currency): return amount
    async def rate(self, currency): return Decimal(1)


def _profile(lang: str = "en") -> UserProfile:
    p = UserProfile(telegram_user_id=1, username="t", first_name="T")
    p.settings.language = Language(lang)
    return p


def _funding_signal() -> Signal:
    # Mirrors assembler._assemble_funding: $1 placeholder leg prices, annualized spread
    # carried separately, buy_exchange = long (low funding), sell_exchange = short (high).
    return Signal(
        arb_type=ArbitrageType.FUNDING, coin="NEAR", trading_pair="NEAR/USDT", network=None,
        buy_exchange="okx", sell_exchange="bitget",
        buy_price=Decimal("1"), sell_price=Decimal("1"),
        buy_venue_type="CEX", sell_venue_type="CEX",
        spread_pct=Decimal("36.57"), net_profit_pct=Decimal("0.25"),
        net_profit_usd=Decimal("5.01"), liquidity_usd=Decimal("2000"),
        risk_score=RiskScore.LOW, confidence_score=76,
        funding_annualized_spread=Decimal("36.57"),
        funding_next_time=time.time() + 3600,
        profit_breakdown=ProfitBreakdown(
            size_usd=Decimal("1000"), gross_profit_usd=Decimal("7"),
            trading_fees_usd=Decimal("2"), withdrawal_fees_usd=Decimal("0"),
            gas_fees_usd=Decimal("0"), bridge_fees_usd=Decimal("0"),
            slippage_cost_usd=Decimal("0"), conversion_cost_usd=Decimal("0"),
            net_profit_usd=Decimal("5.01"), roi_pct=Decimal("0.25"),
            gross_spread_pct=Decimal("36.57"), net_profit_pct=Decimal("0.25")),
        sizing=SizingProfile(Decimal("1000"), Decimal("2000"), Decimal("1000"),
                             Decimal("2000"), [(Decimal("100"), Decimal("0.25"))]),
    )


# ── alert ──────────────────────────────────────────────────────────────────────────────
async def test_funding_alert_uses_long_short_not_buy_sell():
    text = await format_alert(_funding_signal(), _profile(), _Fx())
    assert "Long okx" in text and "Short bitget" in text
    assert "Buy okx" not in text and "Sell bitget" not in text
    assert "$1.00" not in text


async def test_spot_alert_still_uses_buy_sell():
    sig = _funding_signal()
    sig.arb_type = ArbitrageType.CEX_CEX
    text = await format_alert(sig, _profile(), _Fx())
    assert "Buy okx" in text and "Sell bitget" in text


# ── overview (details) ─────────────────────────────────────────────────────────────────
async def test_funding_overview_has_no_placeholder_prices():
    text = await format_details(_funding_signal(), _profile(), _Fx())
    assert "$1.00" not in text
    assert "Long (low funding) on okx" in text
    assert "Short (high funding) on bitget" in text
    assert "Next funding" in text
    # The generic "Buy on … @ …" price line must not appear for funding.
    assert "Buy on okx" not in text


async def test_spot_overview_keeps_prices():
    sig = _funding_signal()
    sig.arb_type = ArbitrageType.CEX_CEX
    sig.buy_price = Decimal("100")
    sig.sell_price = Decimal("102")
    text = await format_details(sig, _profile(), _Fx())
    assert "Buy on okx" in text


# ── execution guide (trade route) ──────────────────────────────────────────────────────
def test_funding_route_is_a_hedge_guide_not_a_transfer():
    route = _trade_route(_funding_signal(), "en")
    assert "Open LONG NEAR perpetual on okx" in route
    assert "Open SHORT NEAR perpetual on bitget" in route
    # No spot transfer instruction ("Move NEAR → …") and no bogus network.
    assert "Move NEAR" not in route
    assert "network:" not in route


@pytest.mark.parametrize("lang", LANGUAGES)
def test_funding_route_localized_no_marker(lang):
    route = _trade_route(_funding_signal(), lang)
    assert "⟦" not in route  # no missing i18n key in any language
