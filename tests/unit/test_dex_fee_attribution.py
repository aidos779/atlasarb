"""A DEX pool fee must be *reported* as a fee, not as slippage.

Follow-up to the double-count fix. Zeroing FeeInputs for DEX legs made net profit right
but left the cost breakdown lying to paying users: trading_fees_usd read $0.00 on every
DEX signal while slippage_cost_usd silently carried the pool's swap fee on top of real
price impact.

The engine now splits the two. The load-bearing property is that this is a pure
*reclassification*: value moves between two ProfitBreakdown fields and nothing else
changes. ``test_reclassification_does_not_move_any_total`` is the guard on that — it
recomputes the pre-split maths inline and demands exact equality of net/ROI.
"""
from decimal import Decimal

import pytest

from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook
from src.scanner.profit.engine import ProfitEngine
from src.scanner.profit.liquidity_leg import CexBookLeg, DexPoolLeg
from src.scanner.profit.models import FeeInputs

DEX_SYM = CanonicalSymbol("ETH", "USDT", VenueType.DEX, "ethereum")
CEX_SYM = CanonicalSymbol("ETH", "USDT", VenueType.CEX)


def _pool(venue, reserve_base, reserve_quote, fee_tier):
    return OrderBook(venue, DEX_SYM, bids=[], asks=[], pool_address=f"{venue}:p",
                     pool_fee_tier=Decimal(fee_tier),
                     reserve_base=Decimal(reserve_base),
                     reserve_quote=Decimal(reserve_quote))


def _cex_book(bid, ask, qty="10000"):
    return OrderBook("cex", CEX_SYM, [BookLevel(Decimal(bid), Decimal(qty))],
                     [BookLevel(Decimal(ask), Decimal(qty))])


def _fees(buy_rate, sell_rate, withdrawal="1", gas="2"):
    return FeeInputs(buy_rate, sell_rate, Decimal(withdrawal), Decimal(gas), None,
                     requires_withdrawal=True, requires_gas=True, requires_bridge=False)


def _legacy_costs(size_usd, buy_leg, sell_leg, fees):
    """The pre-split cost maths, verbatim, as the invariant's reference point.

    Mirrors the engine's *aggregate* model (physical base quantity: base bought at the
    buy fill price, the exact same quantity sold) so the invariant isolates the
    reclassification step alone.
    """
    buy_best, sell_best = buy_leg.best_price(), sell_leg.best_price()
    buy_fill = buy_leg.fill_price(size_usd)
    base_size = size_usd / buy_fill
    sell_notional = base_size * sell_best
    sell_fill = sell_leg.fill_price(sell_notional)
    gross = base_size * (sell_best - buy_best)
    slippage = (max(Decimal(0), (buy_fill - buy_best) * base_size)
                + max(Decimal(0), (sell_best - sell_fill) * base_size))
    trading = (size_usd * (fees.buy_fee_rate or Decimal(0))
               + base_size * sell_fill * (fees.sell_fee_rate or Decimal(0)))
    conversion = size_usd * fees.conversion_cost_bps / Decimal(10000)
    net = (gross - trading - (fees.withdrawal_fee_usd or Decimal(0))
           - (fees.gas_fee_usd or Decimal(0)) - (fees.bridge_fee_usd or Decimal(0))
           - slippage - conversion)
    return {"net": net, "trading": trading, "slippage": slippage,
            "sell_notional": sell_notional, "pct": net / size_usd * Decimal(100)}


# ── the leg-level primitive ──

def test_cex_leg_has_no_fee_baked_into_its_fill():
    """A CEX taker fee settles outside the book, so nothing is hidden in the fill."""
    leg = CexBookLeg(_cex_book("2999", "3000"), "buy")
    assert leg.pool_fee_cost_usd(Decimal("5000")) == Decimal(0)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("tier", ["0.0001", "0.0005", "0.003", "0.01"])
def test_pool_fee_is_the_tier_times_the_input(side, tier):
    """Both sides reduce to fee_rate * size_usd — see DexPoolLeg.pool_fee_cost_usd."""
    leg = DexPoolLeg(_pool("uni", "1000", "3000000", tier), side)
    assert leg.pool_fee_cost_usd(Decimal("5000")) == Decimal(tier) * Decimal("5000")


def test_pool_fee_is_zero_for_a_zero_size():
    leg = DexPoolLeg(_pool("uni", "1000", "3000000", "0.003"), "buy")
    assert leg.pool_fee_cost_usd(Decimal(0)) == Decimal(0)


# ── the split ──

@pytest.mark.parametrize("tier", ["0.0001", "0.0005", "0.003"])
def test_dex_dex_reports_the_real_pool_fee_not_zero(tier):
    engine = ProfitEngine(ScannerConfig())
    buy = DexPoolLeg(_pool("uni", "1000", "3000000", tier), "buy")
    sell = DexPoolLeg(_pool("sushi", "1000", "3090000", tier), "sell")
    size = Decimal("5000")

    bd = engine.compute_at_size(size, buy, sell, _fees(Decimal(0), Decimal(0)))
    assert bd is not None
    # Both legs' pool fees, at the pool's own tier — not a flat 0.3%, and not $0.00.
    # The sell leg's fee applies to its own (physical) notional: base × sell_best.
    # Summed per leg (the engine's association) — one Decimal ulp matters at 28 digits.
    legacy = _legacy_costs(size, buy, sell, _fees(Decimal(0), Decimal(0)))
    assert bd.trading_fees_usd == (Decimal(tier) * size
                                   + Decimal(tier) * legacy["sell_notional"])
    assert bd.trading_fees_usd > 0
    # What is left over is price impact, and it is strictly less than the raw figure
    # the old code reported as "slippage".
    assert bd.slippage_cost_usd < legacy["slippage"]
    assert bd.slippage_cost_usd > 0


def test_small_trade_is_almost_all_fee_and_almost_no_impact():
    """The clearest statement of the bug: at negligible size relative to depth there is
    essentially no price impact, so the cost is the fee — which used to render as
    'Trading fees: $0.00 / Est. slippage: <the fee>'."""
    engine = ProfitEngine(ScannerConfig())
    buy = DexPoolLeg(_pool("uni", "100000", "300000000", "0.003"), "buy")
    sell = DexPoolLeg(_pool("sushi", "100000", "309000000", "0.003"), "sell")
    size = Decimal("10")

    bd = engine.compute_at_size(size, buy, sell, _fees(Decimal(0), Decimal(0)))
    legacy = _legacy_costs(size, buy, sell, _fees(Decimal(0), Decimal(0)))
    assert bd.trading_fees_usd == (Decimal("0.003") * size
                                   + Decimal("0.003") * legacy["sell_notional"])
    assert bd.slippage_cost_usd < bd.trading_fees_usd / 10         # impact is noise
    assert bd.slippage_cost_usd >= 0


def test_cex_dex_sums_one_fee_from_each_path():
    """Mixed case: the CEX leg bills from FeeInputs, the DEX leg from its pool — one
    each, neither dropped, neither counted twice."""
    engine = ProfitEngine(ScannerConfig())
    cex_buy = CexBookLeg(_cex_book("2999", "3000"), "buy")
    dex_sell = DexPoolLeg(_pool("uni", "1000", "3090000", "0.003"), "sell")
    size = Decimal("5000")

    bd = engine.compute_at_size(size, cex_buy, dex_sell,
                                _fees(Decimal("0.001"), Decimal(0)))
    legacy = _legacy_costs(size, cex_buy, dex_sell, _fees(Decimal("0.001"), Decimal(0)))
    cex_component = size * Decimal("0.001")               # taker fee on the spend
    dex_component = Decimal("0.003") * legacy["sell_notional"]  # pool fee on its input
    assert bd.trading_fees_usd == cex_component + dex_component


def test_cex_only_path_is_untouched():
    engine = ProfitEngine(ScannerConfig())
    buy = CexBookLeg(_cex_book("2999", "3000"), "buy")
    sell = CexBookLeg(_cex_book("3050", "3051"), "sell")
    size = Decimal("5000")
    fees = _fees(Decimal("0.001"), Decimal("0.001"))

    bd = engine.compute_at_size(size, buy, sell, fees)
    legacy = _legacy_costs(size, buy, sell, fees)
    assert bd.trading_fees_usd == legacy["trading"]
    assert bd.slippage_cost_usd == legacy["slippage"]


# ── the invariant ──

@pytest.mark.parametrize("tier", ["0.0001", "0.0005", "0.003", "0.01"])
@pytest.mark.parametrize("size", ["10", "500", "5000", "50000"])
@pytest.mark.parametrize("shape", [("1000", "3000000"), ("50", "150000")])
@pytest.mark.parametrize("pair", ["DEX-DEX", "CEX-DEX", "DEX-CEX"])
def test_reclassification_does_not_move_any_total(tier, size, shape, pair):
    """The load-bearing guarantee: only the *split* changes, never a total.

    Exact equality, not approximate — Decimal carries 28 significant digits, and
    re-associating the cost sum is enough to shift the last one. net_profit_usd is
    therefore computed from the aggregate costs before the split is applied.
    """
    engine = ProfitEngine(ScannerConfig())
    reserve_base, reserve_quote = shape
    higher = str(Decimal(reserve_quote) * Decimal("1.03"))
    size_d = Decimal(size)

    if pair == "DEX-DEX":
        buy = DexPoolLeg(_pool("uni", reserve_base, reserve_quote, tier), "buy")
        sell = DexPoolLeg(_pool("sushi", reserve_base, higher, tier), "sell")
        fees = _fees(Decimal(0), Decimal(0))
    elif pair == "CEX-DEX":
        buy = CexBookLeg(_cex_book("2999", "3000"), "buy")
        sell = DexPoolLeg(_pool("sushi", reserve_base, higher, tier), "sell")
        fees = _fees(Decimal("0.001"), Decimal(0))
    else:
        buy = DexPoolLeg(_pool("uni", reserve_base, reserve_quote, tier), "buy")
        sell = CexBookLeg(_cex_book("3200", "3201"), "sell")
        fees = _fees(Decimal(0), Decimal("0.001"))

    bd = engine.compute_at_size(size_d, buy, sell, fees)
    if bd is None:
        pytest.skip("size unfillable for this shape")
    legacy = _legacy_costs(size_d, buy, sell, fees)

    assert bd.net_profit_usd == legacy["net"]
    assert bd.net_profit_pct == legacy["pct"]
    assert bd.roi_pct == legacy["pct"]
    # The two reclassified buckets still hold the same total between them. Exact up to
    # one Decimal ulp: the engine's (rate+pool)+(exec−pool) association can shift the
    # 28th significant digit vs the un-split rate+exec sum; net/ROI stay exact above.
    drift = ((bd.trading_fees_usd + bd.slippage_cost_usd)
             - (legacy["trading"] + legacy["slippage"]))
    assert abs(drift) <= Decimal("1e-18")
    # And the split is never allowed to go negative.
    assert bd.slippage_cost_usd >= 0
    assert bd.trading_fees_usd >= 0


def test_optimize_still_agrees_with_compute_at_size():
    """Sizing sweeps read net_profit_usd/roi_pct, so they must see the same numbers."""
    engine = ProfitEngine(ScannerConfig())
    buy = DexPoolLeg(_pool("uni", "1000", "3000000", "0.003"), "buy")
    sell = DexPoolLeg(_pool("sushi", "1000", "3200000", "0.003"), "sell")
    fees = _fees(Decimal(0), Decimal(0))

    result = engine.optimize(buy, sell, fees, "DEX", "DEX")
    assert result is not None
    bd, _sizing = result
    direct = engine.compute_at_size(bd.size_usd, buy, sell, fees)
    assert direct.net_profit_usd == bd.net_profit_usd
    assert direct.trading_fees_usd == bd.trading_fees_usd
    assert direct.slippage_cost_usd == bd.slippage_cost_usd
