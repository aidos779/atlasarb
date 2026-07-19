"""Funding signal never presents its annualized rate as if it were the profit (audit #6).

`spread_pct` on a funding signal is the ANNUALIZED differential (tens of %); the realized
return is the much smaller net-per-hold. The details view must label them distinctly —
in every language, so the safeguard is asserted against catalog keys rather than English
copy (a Russian user must not see the annualized figure labelled as gross spread either).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.bot.formatters.signal import _spread_line
from src.domain.enums import ArbitrageType
from src.domain.signal import Signal
from src.i18n import LANGUAGES, t


def _sig(arb_type: ArbitrageType, spread_pct: str, net_pct: str) -> Signal:
    return Signal(
        arb_type=arb_type, coin="BNB", trading_pair="BNB/USDT", network=None,
        buy_exchange="okx", sell_exchange="bitget",
        buy_price=Decimal("1"), sell_price=Decimal("1"),
        buy_venue_type="CEX", sell_venue_type="CEX",
        spread_pct=Decimal(spread_pct), net_profit_pct=Decimal(net_pct),
    )


@pytest.mark.parametrize("lang", LANGUAGES)
def test_funding_spread_line_separates_annualized_from_net(lang):
    line = _spread_line(_sig(ArbitrageType.FUNDING, "22.0", "0.057"), lang)
    assert t("details.funding_annualized", lang) in line
    assert t("details.net_per_hold", lang) in line
    # The big annualized number is never labelled "Gross Spread" (would read as profit).
    assert t("details.gross_spread", lang) not in line


@pytest.mark.parametrize("lang", LANGUAGES)
def test_price_arb_spread_line_unchanged(lang):
    line = _spread_line(_sig(ArbitrageType.CEX_CEX, "1.2", "0.4"), lang)
    assert line.startswith(t("details.gross_spread", lang) + ":")
    assert t("details.funding_annualized", lang) not in line
