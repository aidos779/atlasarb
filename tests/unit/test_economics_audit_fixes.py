"""Regression tests for the economics-audit fixes.

Covers, one section per fix:
  P0-1  funding signals run through the shared SignalValidator (no bypass);
  P0-2  per-venue CEX taker rates from central config (no flat hardcode);
  P0-3  liquidity depth normalized against the liquidity reference (no $500 saturation);
  P0-4  liquidity stability: real dispersion or weight redistribution, never a fake 100;
  P1-5  physical base quantity (bought at the buy *fill* price, same quantity sold);
  P1-6  DEX→CEX direction charges the quote-asset return withdrawal (symmetry);
  P1-7  spread-stability confidence factor measures dispersion vs spread, not tick count;
  P1-8  funding liquidity comes from real spot books when present, floor fallback if not;
  P2-9  ReliabilityCache serves sync values over the async hit-rate query;
  P2-11 AMM saturation tolerance measures price impact only (pool fee excluded).
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import pytest

from src.config.scanner_config import ConfigError, ScannerConfig, validate_config
from src.domain.enums import ArbitrageType, ExchangeStatus, RejectReason, VenueType
from src.domain.market import BookLevel, CanonicalSymbol, FundingRate, OrderBook
from src.domain.ports import ExchangeAdapter
from src.scanner import mathx
from src.scanner.assembler import SignalAssembler, _spread_stability, _worst_dispersion_pct
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.liquidity.analyzer import LiquidityAnalyzer
from src.scanner.priority.scheduler import PriorityClassifier
from src.scanner.profit.engine import ProfitEngine
from src.scanner.profit.liquidity_leg import CexBookLeg
from src.scanner.profit.models import FeeInputs
from src.scanner.status.health_registry import HealthRegistry
from src.domain.signal import Candidate, FundingSnapshot, LegRef

CEX_SYM = CanonicalSymbol("ETH", "USDT", VenueType.CEX)


class _Ad(ExchangeAdapter):
    venue_type = VenueType.CEX
    network = None

    def __init__(self, vid, taker="0.001", withdrawal_by_asset=None):
        self.id = vid
        self.display_name = vid
        self._taker = Decimal(taker)
        self._wd = withdrawal_by_asset or {}

    async def connect(self): ...
    async def disconnect(self): ...
    async def get_markets(self): return []
    async def subscribe_ticker(self, s): ...
    async def subscribe_order_book(self, s, d): ...
    async def get_funding_rate(self, a): return None
    async def get_pool_state(self, s): return None
    async def health_check(self): return True
    def get_status(self): return ExchangeStatus.ONLINE
    def taker_fee(self, s): return self._taker
    def withdrawal_fee_usd(self, a, n): return self._wd.get(a, Decimal("1"))
    def withdrawals_enabled(self, a, n): return True


class _DexAd(_Ad):
    venue_type = VenueType.DEX
    network = "ethereum"


class _Gas:
    async def gas_price_usd(self, n, u): return Decimal("0.5")


def _book(venue, bid, ask, qty="1000"):
    return OrderBook(venue, CEX_SYM,
                     [BookLevel(Decimal(bid), Decimal(qty))],
                     [BookLevel(Decimal(ask), Decimal(qty))])


def _online(health, *venues):
    for v in venues:
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)


def _funding_candidate(now, *, age_sec=0.0, history=(Decimal("0.0005"),) * 5,
                       has_predicted=True, has_next_time=True,
                       annualized=Decimal("0.45")):
    snap = FundingSnapshot(received_at=now - age_sec, current_rate=Decimal("0.0005"),
                           has_predicted=has_predicted, has_next_time=has_next_time,
                           history=tuple(history))
    return Candidate(
        arb_type=ArbitrageType.FUNDING, base_asset="ETH", quote_asset="USDT",
        buy_leg=LegRef("binance", "CEX", Decimal(1)),
        sell_leg=LegRef("okx", "CEX", Decimal(1)),
        gross_spread_pct=annualized * 100,
        funding_annualized_spread=annualized,
        funding_next_time=now + 3600, funding_low=snap, funding_high=snap,
    )


def _funding_assembler(cfg=None):
    cfg = cfg or ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    adapters = {"binance": _Ad("binance"), "okx": _Ad("okx")}
    _online(health, *adapters)
    asm = SignalAssembler(cfg, cache, health, adapters, _Gas(), PriorityClassifier(cfg))
    return asm, cache, health, cfg


# ── P0-1: funding runs through the shared SignalValidator ──────────────────────────────

async def test_funding_publishes_when_all_gates_pass():
    asm, _cache, _health, _cfg = _funding_assembler()
    res = await asm.assemble(_funding_candidate(time.time()))
    assert res.signal is not None


async def test_funding_rejected_on_low_confidence():
    """Poor funding-data quality (stale-ish, incomplete, unstable) must fail Gate 7 —
    previously funding computed a confidence score and never compared it to anything."""
    cfg = ScannerConfig()
    asm, _cache, _health, _ = _funding_assembler(cfg)
    # Freshness ~10 (age 90% of horizon), stability 50 (1 sample), completeness 50.
    cand = _funding_candidate(time.time(), age_sec=cfg.max_age_funding_sec * 0.9,
                              history=(Decimal("0.0005"),),
                              has_predicted=False, has_next_time=False)
    res = await asm.assemble(cand)
    assert res.signal is None
    assert res.reject_reason == RejectReason.LOW_CONFIDENCE


async def test_funding_rejected_on_stale_data():
    cfg = ScannerConfig()
    asm, _cache, _health, _ = _funding_assembler(cfg)
    cand = _funding_candidate(time.time(), age_sec=cfg.max_age_funding_sec + 5)
    res = await asm.assemble(cand)
    assert res.signal is None
    assert res.reject_reason == RejectReason.STALE_DATA


async def test_funding_rejected_when_exchange_not_online():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    adapters = {"binance": _Ad("binance"), "okx": _Ad("okx")}
    _online(health, "binance")
    health.register("okx")  # UNKNOWN — not signal-eligible
    asm = SignalAssembler(cfg, cache, health, adapters, _Gas(), PriorityClassifier(cfg))
    res = await asm.assemble(_funding_candidate(time.time()))
    assert res.signal is None
    assert res.reject_reason == RejectReason.EXCHANGE_NOT_ONLINE


async def test_funding_rejected_below_min_roi():
    """The §10 ROI bar now applies to funding: net can clear min_net_profit_usd while
    ROI on the 2x-margin capital stays under the floor."""
    cfg = ScannerConfig()
    cfg.min_roi_pct = 0.5  # funding roi = net / (2 × size) × 100
    asm, _cache, _health, _ = _funding_assembler(cfg)
    # annualized 0.45 → gross ≈ $8.63, net ≈ $6.63 → roi ≈ 0.33% < 0.5%.
    res = await asm.assemble(_funding_candidate(time.time()))
    assert res.signal is None
    assert res.reject_reason == RejectReason.BELOW_MIN_PROFIT


# ── P0-2: per-venue taker rates ────────────────────────────────────────────────────────

def test_config_serves_per_venue_taker_rates():
    cfg = ScannerConfig()
    assert cfg.cex_taker_fee("mexc") == 0.0005
    assert cfg.cex_taker_fee("binance") == 0.001
    assert cfg.cex_taker_fee("unknown-venue") == cfg.cex_taker_fee_default


def test_config_rejects_insane_taker_rate():
    cfg = ScannerConfig()
    cfg.cex_taker_fee_by_venue = {"binance": 0.1}  # 10% — clearly a percent, not fraction
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_base_cex_adapter_reads_rate_from_config():
    from src.config.settings import Settings
    from src.scanner.adapters.base_cex import BaseCexAdapter

    class _Mexc(BaseCexAdapter):
        id = "mexc"
        display_name = "mexc"

        async def _fetch_markets(self, session): return []
        def _subscribe_frames(self, symbols): return []
        def _parse_message(self, message): return []
        async def _fetch_funding(self, session, base_asset): return None

    cfg = ScannerConfig()
    a = _Mexc(Settings(), cfg, sink=None, rest_url="http://x", ws_url="ws://x",
              rate_per_sec=100, burst=100)
    assert a.taker_fee(CEX_SYM) == Decimal("0.0005")

    cfg2 = ScannerConfig()
    cfg2.cex_taker_fee_by_venue = {"mexc": 0.002}
    b = _Mexc(Settings(), cfg2, sink=None, rest_url="http://x", ws_url="ws://x",
              rate_per_sec=100, burst=100)
    assert b.taker_fee(CEX_SYM) == Decimal("0.002")


# ── P0-3: liquidity depth reference ────────────────────────────────────────────────────

def test_depth_no_longer_saturates_at_500_usd():
    """With the profit reference ($500) any book scored 100; with the liquidity
    reference a $10k-depth book must score visibly below a $250k-depth one."""
    cfg = ScannerConfig()
    an = LiquidityAnalyzer(cfg)
    ref = Decimal(str(cfg.liquidity_reference_usd(1)))  # majors: $250k

    def legs(qty):
        return (CexBookLeg(_book("a", "2999", "3000", qty), "buy"),
                CexBookLeg(_book("b", "3050", "3051", qty), "sell"))

    thin = an.score(*legs("3.34"), "CEX", "CEX", ref)      # ≈ $10k depth
    deep = an.score(*legs("100"), "CEX", "CEX", ref)       # ≈ $300k depth (capped)
    assert deep > thin
    assert thin < Decimal(60)   # not pinned at the ceiling
    assert deep > Decimal(90)


def test_liquidity_reference_is_config_tunable_per_tier():
    cfg = ScannerConfig()
    assert cfg.liquidity_reference_usd(1) == cfg.liquidity_ref_p1_usd
    assert cfg.liquidity_reference_usd(3) == cfg.liquidity_ref_p3_usd
    assert cfg.liquidity_reference_usd(99) == cfg.liquidity_ref_p3_usd
    assert cfg.liquidity_ref_p1_usd > cfg.liquidity_ref_p3_usd


# ── P0-4: stability — real value or weight redistribution ──────────────────────────────

def test_no_history_redistributes_weight_instead_of_fake_100():
    cfg = ScannerConfig()
    an = LiquidityAnalyzer(cfg)
    buy = CexBookLeg(_book("a", "2999", "3000", "100"), "buy")
    sell = CexBookLeg(_book("b", "3050", "3051", "100"), "sell")
    ref = Decimal("250000")
    no_history = an.score(buy, sell, "CEX", "CEX", ref, None)
    perfect = an.score(buy, sell, "CEX", "CEX", ref, Decimal(0))
    # Renormalized over depth+balance — NOT the old behavior of crediting a full
    # stability 100 (which `perfect` explicitly does): identical only if depth and
    # balance both also sit at their own extremes.
    w_d, w_b = Decimal(str(cfg.lw_depth)), Decimal(str(cfg.lw_balance))
    depth_balance_only = no_history * (w_d + w_b)
    with_fake_100 = depth_balance_only + Decimal(str(cfg.lw_stability)) * Decimal(100)
    assert perfect == pytest.approx(with_fake_100, abs=1e-18)
    assert no_history != perfect


def test_high_dispersion_lowers_liquidity_score():
    cfg = ScannerConfig()
    an = LiquidityAnalyzer(cfg)
    buy = CexBookLeg(_book("a", "2999", "3000", "100"), "buy")
    sell = CexBookLeg(_book("b", "3050", "3051", "100"), "sell")
    ref = Decimal("250000")
    calm = an.score(buy, sell, "CEX", "CEX", ref, Decimal("1"))
    wild = an.score(buy, sell, "CEX", "CEX", ref, Decimal("40"))
    assert calm > wild


# ── P1-5: physical base quantity ───────────────────────────────────────────────────────

def _steep_buy_book():
    # Thin best level → significant buy-side slippage at size $10k.
    return OrderBook("v", CEX_SYM,
                     bids=[BookLevel(Decimal("99"), Decimal("1000"))],
                     asks=[BookLevel(Decimal("100"), Decimal("20")),
                           BookLevel(Decimal("103"), Decimal("1000"))])


def test_base_quantity_uses_the_buy_fill_price():
    engine = ProfitEngine(ScannerConfig())
    buy = CexBookLeg(_steep_buy_book(), "buy")
    sell = CexBookLeg(_book("s", "105", "106", "1000"), "sell")
    size = Decimal("10000")
    fees = FeeInputs(Decimal(0), Decimal(0), Decimal(0), Decimal(0), None,
                     requires_withdrawal=False, requires_gas=False, requires_bridge=False)
    bd = engine.compute_at_size(size, buy, sell, fees)
    assert bd is not None

    buy_fill = buy.fill_price(size)
    base = size / buy_fill                       # physical quantity actually held
    sell_fill = sell.fill_price(base * sell.best_price())
    # Exact P&L identity on the physical quantity: proceeds − cost == net (no fees here).
    assert bd.net_profit_usd == base * sell_fill - size
    # The old model sized the position at size/buy_best — strictly more base than the
    # spend can buy on this book, i.e. an optimistic net. Guard the direction:
    optimistic_base = size / buy.best_price()
    assert base < optimistic_base


def test_sell_fee_is_charged_on_actual_proceeds():
    engine = ProfitEngine(ScannerConfig())
    buy = CexBookLeg(_book("b", "99", "100", "1000"), "buy")
    sell = CexBookLeg(_book("s", "105", "106", "1000"), "sell")
    size = Decimal("10000")
    rate = Decimal("0.001")
    fees = FeeInputs(rate, rate, Decimal(0), Decimal(0), None,
                     requires_withdrawal=False, requires_gas=False, requires_bridge=False)
    bd = engine.compute_at_size(size, buy, sell, fees)
    buy_fill = buy.fill_price(size)
    base = size / buy_fill
    sell_fill = sell.fill_price(base * sell.best_price())
    assert bd.trading_fees_usd == size * rate + base * sell_fill * rate


# ── P1-6: DEX→CEX withdrawal symmetry ─────────────────────────────────────────────────

def _cexdex_assembler(withdrawals):
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    adapters = {"binance": _Ad("binance", withdrawal_by_asset=withdrawals),
                "uni": _DexAd("uni")}
    _online(health, *adapters)
    return SignalAssembler(cfg, cache, health, adapters, _Gas(), PriorityClassifier(cfg))


async def test_buy_dex_sell_cex_charges_quote_return_withdrawal():
    wd = {"USDT": Decimal("7"), "ETH": Decimal("3")}
    asm = _cexdex_assembler(wd)
    cand = Candidate(
        arb_type=ArbitrageType.CEX_DEX, base_asset="ETH", quote_asset="USDT",
        buy_leg=LegRef("uni", "DEX", Decimal("3000"), network="ethereum"),
        sell_leg=LegRef("binance", "CEX", Decimal("3050")),
        gross_spread_pct=Decimal("1.6"))
    fees = await asm._resolve_fees(cand)
    # Return of capital: one quote-asset (USDT) withdrawal from the sell CEX — was 0.
    assert fees.withdrawal_fee_usd == Decimal("7")


async def test_buy_cex_sell_dex_direction_is_unchanged():
    wd = {"USDT": Decimal("7"), "ETH": Decimal("3")}
    asm = _cexdex_assembler(wd)
    cand = Candidate(
        arb_type=ArbitrageType.CEX_DEX, base_asset="ETH", quote_asset="USDT",
        buy_leg=LegRef("binance", "CEX", Decimal("3000")),
        sell_leg=LegRef("uni", "DEX", Decimal("3050"), network="ethereum"),
        gross_spread_pct=Decimal("1.6"))
    fees = await asm._resolve_fees(cand)
    # Base asset withdrawn from the buy CEX toward the chain, as before.
    assert fees.withdrawal_fee_usd == Decimal("3")


# ── P1-7: spread stability is dispersion-vs-spread, not tick count ────────────────────

def test_spread_stability_scales_with_dispersion():
    spread = Decimal("1.0")
    assert _spread_stability(Decimal("0"), spread) == Decimal(100)
    assert _spread_stability(Decimal("0.5"), spread) == Decimal(50)
    assert _spread_stability(Decimal("1.0"), spread) == Decimal(0)
    assert _spread_stability(Decimal("5.0"), spread) == Decimal(0)   # clamped


def test_spread_stability_missing_history_is_penalized_not_perfect():
    # < 3 samples used to yield stability from tick *count*; now it is the same
    # conservative 50 the funding stability factor uses for missing data.
    assert _spread_stability(None, Decimal("1.0")) == Decimal(50)


def test_worst_dispersion_takes_the_noisier_leg():
    calm = [Decimal("100"), Decimal("100.1"), Decimal("99.9"), Decimal("100")]
    wild = [Decimal("100"), Decimal("110"), Decimal("90"), Decimal("100")]
    d = _worst_dispersion_pct(calm, wild)
    assert d == _worst_dispersion_pct(wild, calm)          # order-independent
    assert d > _worst_dispersion_pct(calm, calm)
    assert _worst_dispersion_pct([], []) is None           # no history at all


# ── P1-8: funding liquidity — real books first, floor fallback ────────────────────────

async def test_funding_uses_real_spot_books_when_available():
    asm, cache, _health, cfg = _funding_assembler()
    cache.track("binance", CEX_SYM)
    cache.track("okx", CEX_SYM)
    cache.upsert_book(_book("binance", "2999", "3000", "1000"))
    cache.upsert_book(_book("okx", "3000", "3001", "1000"))
    res = await asm.assemble(_funding_candidate(time.time()))
    assert res.signal is not None
    # Real executable depth from the books — not the old synthetic 2×size ($2000).
    assert res.signal.liquidity_usd > Decimal("100000")


async def test_funding_without_books_falls_back_to_floors():
    asm, _cache, _health, cfg = _funding_assembler()
    res = await asm.assemble(_funding_candidate(time.time()))
    assert res.signal is not None
    assert res.signal.liquidity_usd == Decimal(str(cfg.min_liquidity_cex_usd))


# ── P2-9: ReliabilityCache ─────────────────────────────────────────────────────────────

async def test_reliability_cache_serves_default_then_cached_value():
    from src.services.history_service import ReliabilityCache

    class _Hist:
        calls = 0
        async def reliability(self, arb_type, buy, sell):
            self.calls += 1
            return 85.0

    hist = _Hist()
    cache = ReliabilityCache(hist, ttl_sec=600.0)
    first = cache("CEX_CEX", "a", "b")
    assert first == Decimal("60")            # default until the async refresh lands
    await asyncio.sleep(0)                   # let the fire-and-forget task run
    await asyncio.sleep(0)
    assert cache("CEX_CEX", "a", "b") == Decimal("85")
    assert hist.calls == 1                   # deduped in-flight, then TTL-cached


# ── P2-11: AMM saturation measures impact only ────────────────────────────────────────

def test_amm_max_size_tolerance_not_consumed_by_pool_fee():
    rb, rq = Decimal("1000"), Decimal("3000000")
    tol = Decimal("0.5")
    # A 1% fee tier exceeds the 0.5% tolerance at size zero: the old baseline (raw
    # spot) returned ~0. Impact-only semantics must admit a real size.
    high_tier = mathx.amm_max_size_within_slippage(rq, rb, Decimal("0.01"), tol)
    assert high_tier > rq / Decimal(1000)   # clearly non-degenerate
    # And the returned size's *impact* (vs the zero-size effective price) is at
    # the tolerance boundary, not beyond it.
    zero_size_price = (rq / rb) / (Decimal(1) - Decimal("0.01"))
    eff = mathx.amm_effective_price(rq, rb, high_tier, Decimal("0.01"))
    assert mathx.slippage_pct(eff, zero_size_price) <= tol * Decimal("1.0001")


def test_amm_max_size_still_monotonic_in_tolerance():
    rb, rq = Decimal("1000"), Decimal("3000000")
    small = mathx.amm_max_size_within_slippage(rq, rb, Decimal("0.003"), Decimal("0.5"))
    large = mathx.amm_max_size_within_slippage(rq, rb, Decimal("0.003"), Decimal("1.5"))
    assert large > small > 0


# ── Production-review fixes ───────────────────────────────────────────────────────────

async def test_funding_thin_spot_book_is_not_stricter_than_no_book():
    """Monotone proxy evidence (fix E2): a thin real spot book must never reject a
    funding signal that the no-book neutral fallback would have passed."""
    asm, cache, _health, cfg = _funding_assembler()
    cache.track("binance", CEX_SYM)
    cache.track("okx", CEX_SYM)
    # ~$300 executable depth — far under the $5k floor.
    cache.upsert_book(_book("binance", "2999", "3000", "0.1"))
    cache.upsert_book(_book("okx", "3000", "3001", "0.1"))
    res = await asm.assemble(_funding_candidate(time.time()))
    assert res.signal is not None                       # not INSUFFICIENT_LIQUIDITY
    assert res.signal.liquidity_usd == Decimal(str(cfg.min_liquidity_cex_usd))


async def test_funding_deep_spot_book_still_earns_real_credit():
    asm, cache, _health, cfg = _funding_assembler()
    cache.track("binance", CEX_SYM)
    cache.track("okx", CEX_SYM)
    cache.upsert_book(_book("binance", "2999", "3000", "1000"))
    cache.upsert_book(_book("okx", "3000", "3001", "1000"))
    res = await asm.assemble(_funding_candidate(time.time()))
    assert res.signal is not None
    assert res.signal.liquidity_usd > Decimal(str(cfg.min_liquidity_cex_usd))


def test_cooldown_store_purges_elapsed_entries():
    from src.domain.enums import ArbitrageType as AT
    from src.scanner.lifecycle.cooldown import CooldownStore
    cfg = ScannerConfig()
    store = CooldownStore(cfg)
    t0 = 1_000_000.0
    # Fill past the purge threshold with entries whose cooldown elapses immediately.
    for i in range(store._PURGE_THRESHOLD + 10):
        store.on_expired(("CEX_CEX", f"C{i}", "USDT", ("a", "b"), None), AT.CEX_CEX, t0)
    # All elapsed by t1; the next on_expired must sweep them out instead of growing.
    t1 = t0 + cfg.cooldown_for("CEX_CEX") + 1
    store.on_expired(("CEX_CEX", "FRESH", "USDT", ("a", "b"), None), AT.CEX_CEX, t1)
    assert len(store._cooldowns) == 1                   # only the fresh entry survives
    assert store.in_cooldown(("CEX_CEX", "FRESH", "USDT", ("a", "b"), None), t1)


def test_cache_untrack_clears_suspect_flag():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    cache.track("binance", CEX_SYM)
    cache._suspect[("binance", "ETH/USDT")] = True
    cache.untrack("binance", "ETH/USDT")
    assert ("binance", "ETH/USDT") not in cache._suspect
