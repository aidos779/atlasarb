"""Funding signal never presents its annualized rate as if it were the profit (audit #6).

`spread_pct` on a funding signal is the ANNUALIZED differential (tens of %); the realized
return is the much smaller net-per-hold. The details view must label them distinctly.
"""
from __future__ import annotations

from decimal import Decimal

from src.bot.formatters.signal import _spread_line
from src.domain.enums import ArbitrageType
from src.domain.signal import Signal


def _sig(arb_type: ArbitrageType, spread_pct: str, net_pct: str) -> Signal:
    return Signal(
        arb_type=arb_type, coin="BNB", trading_pair="BNB/USDT", network=None,
        buy_exchange="okx", sell_exchange="bitget",
        buy_price=Decimal("1"), sell_price=Decimal("1"),
        buy_venue_type="CEX", sell_venue_type="CEX",
        spread_pct=Decimal(spread_pct), net_profit_pct=Decimal(net_pct),
    )


def test_funding_spread_line_separates_annualized_from_net():
    line = _spread_line(_sig(ArbitrageType.FUNDING, "22.0", "0.057"))
    assert "Annualized funding" in line
    assert "Net/hold" in line
    # The big annualized number is never labelled "Gross Spread" (would read as profit).
    assert "Gross Spread" not in line


def test_price_arb_spread_line_unchanged():
    line = _spread_line(_sig(ArbitrageType.CEX_CEX, "1.2", "0.4"))
    assert line.startswith("Gross Spread:")
    assert "Annualized" not in line
