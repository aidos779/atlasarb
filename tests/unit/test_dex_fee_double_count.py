"""Regression: a DEX pool fee must be charged exactly once.

The pool fee is taken out of the input amount inside the AMM, so
``DexPoolLeg.fill_price`` (via ``mathx.amm_output``) already prices it in and the engine
picks it up as slippage_cost. The assembler used to *also* charge
``adapter.taker_fee()`` on top for DEX legs — a flat 0.3% regardless of the pool's real
tier — so every DEX leg paid its fee twice, and low-tier V3 pools (0.05% / 0.01%) paid a
second charge 6-30x larger than the first. That sank most DEX_DEX / CROSS_CHAIN
candidates and the DEX side of CEX_DEX into UNPROFITABLE_AFTER_FEES / BELOW_MIN_PROFIT.
"""
from decimal import Decimal

import pytest

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, ExchangeStatus, VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook
from src.domain.ports import ExchangeAdapter
from src.scanner.assembler import _charges_flat_taker_fee
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


def _cex_book(bid, ask, qty="1000"):
    return OrderBook("cex", CEX_SYM, [BookLevel(Decimal(bid), Decimal(qty))],
                     [BookLevel(Decimal(ask), Decimal(qty))])


def _fees(buy_rate, sell_rate):
    return FeeInputs(buy_rate, sell_rate, Decimal(0), Decimal(0), None,
                     requires_withdrawal=False, requires_gas=False,
                     requires_bridge=False)


# ── the rule ──

def test_dex_legs_do_not_charge_a_flat_taker_fee():
    assert _charges_flat_taker_fee("DEX") is False
    assert _charges_flat_taker_fee("CEX") is True


# ── the maths ──

def test_pool_fee_is_already_inside_the_fill_price():
    """Sanity anchor for the whole fix: fill_price moves with the pool's fee tier."""
    cheap = DexPoolLeg(_pool("a", "1000", "3000000", "0.0005"), "buy")
    dear = DexPoolLeg(_pool("b", "1000", "3000000", "0.003"), "buy")
    size = Decimal("10000")
    # Same reserves, different tier -> the costlier pool fills worse.
    assert dear.fill_price(size) > cheap.fill_price(size)
    # And neither equals the fee-free spot price.
    assert cheap.fill_price(size) > cheap.best_price()


@pytest.mark.parametrize("fee_tier", ["0.0001", "0.0005", "0.003"])
def test_dex_dex_charges_the_pool_fee_exactly_once(fee_tier):
    """With DEX rates zeroed, the pool's fee is charged once — at its own tier.

    (Which of the two reported buckets that single charge lands in is the separate
    concern of test_dex_fee_attribution; here the question is only how many times the
    engine deducts it from net profit.)
    """
    engine = ProfitEngine(ScannerConfig())
    buy = DexPoolLeg(_pool("uni", "1000", "3000000", fee_tier), "buy")
    sell = DexPoolLeg(_pool("sushi", "1000", "3090000", fee_tier), "sell")
    size = Decimal("5000")

    correct = engine.compute_at_size(size, buy, sell, _fees(Decimal(0), Decimal(0)))
    assert correct is not None
    # Physical quantities as the engine computes them (base bought at the buy fill,
    # the same quantity sold): the sell pool's fee applies to its own input notional.
    buy_fill = buy.fill_price(size)
    base_size = size / buy_fill
    sell_notional = base_size * sell.best_price()
    sell_fill = sell.fill_price(sell_notional)
    # Counted once, at the pool's real tier — never a flat 0.3%.
    assert correct.trading_fees_usd == (Decimal(fee_tier) * size
                                        + Decimal(fee_tier) * sell_notional)

    # What the old code did: flat 0.3% per leg *on top of* the AMM-priced fill.
    doubled = engine.compute_at_size(
        size, buy, sell, _fees(Decimal("0.003"), Decimal("0.003")))
    assert doubled is not None
    assert doubled.net_profit_usd < correct.net_profit_usd

    # The entire difference is the spurious rate-based second charge, nothing else:
    # the pool fee itself and the price impact are identical in both runs. Exact up to
    # one Decimal ulp — the sequential subtractions in net round at 28 digits, so the
    # difference of two nets can drift in the last digit vs the standalone product.
    spurious = (size * Decimal("0.003")
                + base_size * sell_fill * Decimal("0.003"))
    assert abs((correct.net_profit_usd - doubled.net_profit_usd) - spurious) \
        <= Decimal("1e-18")
    assert abs((doubled.trading_fees_usd - correct.trading_fees_usd) - spurious) \
        <= Decimal("1e-18")
    assert correct.slippage_cost_usd == doubled.slippage_cost_usd


def test_low_tier_v3_pool_was_penalised_worst():
    """0.05% pool charged a flat 0.3% second time = 6x its real fee."""
    engine = ProfitEngine(ScannerConfig())
    buy = DexPoolLeg(_pool("uni", "1000", "3000000", "0.0005"), "buy")
    sell = DexPoolLeg(_pool("sushi", "1000", "3090000", "0.0005"), "sell")
    size = Decimal("5000")

    correct = engine.compute_at_size(size, buy, sell, _fees(Decimal(0), Decimal(0)))
    doubled = engine.compute_at_size(
        size, buy, sell, _fees(Decimal("0.003"), Decimal("0.003")))
    # The bogus charge dwarfs the real pool fee it duplicated.
    assert doubled.trading_fees_usd > correct.slippage_cost_usd


def test_cex_dex_keeps_the_cex_rate_and_drops_the_dex_one():
    """CEX fees settle outside the book, so the rate must still be charged explicitly;
    the DEX leg's flat adapter rate must not be, because its pool already charged it."""
    engine = ProfitEngine(ScannerConfig())
    cex_buy = CexBookLeg(_cex_book("2999", "3000"), "buy")
    dex_sell = DexPoolLeg(_pool("uni", "1000", "3090000", "0.003"), "sell")
    size = Decimal("5000")

    bd = engine.compute_at_size(size, cex_buy, dex_sell,
                                _fees(Decimal("0.001"), Decimal(0)))
    assert bd is not None
    buy_fill = cex_buy.fill_price(size)
    base_size = size / buy_fill
    sell_notional = base_size * dex_sell.best_price()
    sell_fill = dex_sell.fill_price(sell_notional)
    cex_rate_fee = size * Decimal("0.001")            # taker fee on the spend
    pool_fee = Decimal("0.003") * sell_notional       # pool fee on its own input
    # One rate-based charge (CEX) + one pool charge (DEX). Never the DEX rate too.
    assert bd.trading_fees_usd == cex_rate_fee + pool_fee

    # Had the DEX rate not been zeroed, net would be lower by exactly that flat charge
    # (the rate applies to the sell leg's actual proceeds, base × sell_fill).
    doubled = engine.compute_at_size(size, cex_buy, dex_sell,
                                     _fees(Decimal("0.001"), Decimal("0.003")))
    assert (bd.net_profit_usd - doubled.net_profit_usd
            == base_size * sell_fill * Decimal("0.003"))


# ── the assembler wiring ──

class _Ad(ExchangeAdapter):
    """Adapter whose taker_fee is loud enough to spot if it leaks into a DEX leg."""

    network = None

    def __init__(self, vid, venue_type):
        self.id = vid
        self.display_name = vid
        self.venue_type = venue_type

    async def connect(self): ...
    async def disconnect(self): ...
    async def get_markets(self): return []
    async def subscribe_ticker(self, s): ...
    async def subscribe_order_book(self, s, d): ...
    async def get_funding_rate(self, a): return None
    async def get_pool_state(self, s): return None
    async def health_check(self): return True
    def get_status(self): return ExchangeStatus.ONLINE
    def taker_fee(self, s): return Decimal("0.003")
    def withdrawal_fee_usd(self, a, n): return Decimal(0)
    def withdrawals_enabled(self, a, n): return True


class _Gas:
    async def gas_price_usd(self, network, units): return Decimal(0)


def _assembler(adapters):
    from src.scanner.assembler import SignalAssembler
    from src.scanner.cache.market_state_cache import MarketStateCache
    from src.scanner.priority.scheduler import PriorityClassifier
    from src.scanner.status.health_registry import HealthRegistry

    cfg = ScannerConfig()
    return SignalAssembler(cfg, MarketStateCache(cfg), HealthRegistry(cfg), adapters,
                           _Gas(), PriorityClassifier(cfg))


def _candidate(arb_type, buy_vt, sell_vt):
    from src.domain.signal import Candidate, LegRef
    return Candidate(
        arb_type=arb_type, base_asset="ETH", quote_asset="USDT",
        buy_leg=LegRef(venue="buyv", venue_type=buy_vt, price=Decimal("3000"),
                       network="ethereum"),
        sell_leg=LegRef(venue="sellv", venue_type=sell_vt, price=Decimal("3015"),
                        network="ethereum"),
        gross_spread_pct=Decimal("0.5"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("arb_type,buy_vt,sell_vt,expect_buy,expect_sell", [
    (ArbitrageType.CEX_CEX, "CEX", "CEX", Decimal("0.003"), Decimal("0.003")),
    (ArbitrageType.CEX_DEX, "CEX", "DEX", Decimal("0.003"), Decimal(0)),
    (ArbitrageType.CEX_DEX, "DEX", "CEX", Decimal(0), Decimal("0.003")),
    (ArbitrageType.DEX_DEX, "DEX", "DEX", Decimal(0), Decimal(0)),
    (ArbitrageType.CROSS_CHAIN, "DEX", "DEX", Decimal(0), Decimal(0)),
])
async def test_resolve_fees_zeroes_only_dex_legs(arb_type, buy_vt, sell_vt,
                                                 expect_buy, expect_sell):
    adapters = {"buyv": _Ad("buyv", VenueType(buy_vt)),
                "sellv": _Ad("sellv", VenueType(sell_vt))}
    asm = _assembler(adapters)
    fees = await asm._resolve_fees(_candidate(arb_type, buy_vt, sell_vt))

    assert fees.buy_fee_rate == expect_buy
    assert fees.sell_fee_rate == expect_sell
    # Zero, never None — a None rate reads as unresolved and trips MISSING_FEE_DATA.
    assert fees.buy_fee_rate is not None and fees.sell_fee_rate is not None
    assert "trading" in fees.resolved_categories()


@pytest.mark.parametrize("buy_vt,sell_vt,expected_pct", [
    ("CEX", "CEX", Decimal("0.6")),    # both legs charge 0.3%
    ("CEX", "DEX", Decimal("0.3")),    # CEX leg only
    ("DEX", "DEX", Decimal("0")),      # neither
])
def test_fee_floor_excludes_dex_legs(buy_vt, sell_vt, expected_pct):
    """The pre-gate must not reject on a fee it does not model — the pool fee is
    size-dependent and shows up as slippage, which this bound deliberately omits."""
    adapters = {"buyv": _Ad("buyv", VenueType(buy_vt)),
                "sellv": _Ad("sellv", VenueType(sell_vt))}
    asm = _assembler(adapters)
    floor = asm._fee_floor_pct(_candidate(ArbitrageType.CEX_DEX, buy_vt, sell_vt))
    assert floor == expected_pct


def test_dex_dex_candidate_survives_the_pre_gate():
    """A 0.5% DEX-DEX spread used to be killed by a phantom 0.6% fee floor."""
    adapters = {"buyv": _Ad("buyv", VenueType.DEX), "sellv": _Ad("sellv", VenueType.DEX)}
    asm = _assembler(adapters)
    cand = _candidate(ArbitrageType.DEX_DEX, "DEX", "DEX")
    floor = asm._fee_floor_pct(cand)
    assert cand.gross_spread_pct > floor
