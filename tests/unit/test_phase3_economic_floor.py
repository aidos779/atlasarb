"""Phase 3 — detector-side economic filtering.

Detectors reject economically-impossible opportunities before Candidate creation using the
same size-independent lower bound the assembler pre-gate uses, so they drop only what the
assembler would reject (safety invariant), while the assembler pre-gate stays as the final
net. Covers per-detector floors, exact boundaries, the funding economic breakeven, backward
compatibility, the active-route (§12.4) exemption, and that the assembler still catches.
"""
from __future__ import annotations

import time
from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, ExchangeStatus, VenueType
from src.domain.market import (
    BookLevel,
    CanonicalSymbol,
    FundingRate,
    OrderBook,
    PriceQuote,
)
from src.domain.ports import ExchangeAdapter
from src.domain.signal import Candidate, LegRef
from src.scanner.assembler import SignalAssembler
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.detectors.base import DetectionContext, VenueInfo
from src.scanner.detectors.bridges import BridgeRegistry
from src.scanner.detectors.cex_cex import CexCexDetector
from src.scanner.detectors.cross_chain import CrossChainDetector
from src.scanner.detectors.dex_dex import DexDexDetector
from src.scanner.detectors.funding import FundingDetector
from src.scanner.priority.scheduler import PriorityClassifier
from src.scanner.status.health_registry import HealthRegistry

TAKER = Decimal("0.001")


def _health(cfg, venues):
    h = HealthRegistry(cfg)
    for v in venues:
        h.register(v)
        for _ in range(3):
            h.record_success(v, stream=True)
    return h


def _econ_ctx(cfg, venues, venue_type=VenueType.CEX, network=None, **over):
    kw = dict(
        cache=MarketStateCache(cfg),
        health=_health(cfg, venues),
        venues={v: VenueInfo(id=v, venue_type=venue_type, network=network) for v in venues},
        verified_tokens={"ETH", "BTC"},
        economic_floor_enabled=True,
        min_roi_pct=Decimal("0.15"),
        cex_taker_rates={v: TAKER for v in venues} if venue_type == VenueType.CEX else {},
        cost_amortization_usd=Decimal("1000000"),
        gas_estimate_usd=Decimal("1"),
        funding_min_annualized_spread=Decimal("0.05"),
        min_net_profit_usd=Decimal("5"),
        funding_position_size_usd=Decimal("1000"),
        funding_hold_hours=Decimal("168"),
    )
    kw.update(over)
    ctx = DetectionContext(**kw)
    return ctx, kw["cache"]


def _cex(cache, venue, base, bid, ask):
    sym = CanonicalSymbol(base, "USDT", VenueType.CEX)
    cache.track(venue, sym)
    cache.upsert_price(PriceQuote(venue, sym, Decimal(bid), Decimal(ask), Decimal(bid)))
    cache.upsert_book(OrderBook(venue, sym, [BookLevel(Decimal(bid), Decimal("1e9"))],
                                [BookLevel(Decimal(ask), Decimal("1e9"))]))


# ── CEX↔CEX floor + exact boundary ──────────────────────────────────────────────────────
def test_cex_cex_floor_boundary():
    # floor = 2×taker×100 + min_roi = 0.20 + 0.15 = 0.35%.
    cfg = ScannerConfig()
    # below floor → pruned
    ctx, cache = _econ_ctx(cfg, ("binance", "okx"))
    _cex(cache, "binance", "BTC", "99.9", "100.00")
    _cex(cache, "okx", "BTC", "100.30", "100.40")   # best_bid 100.30 → gross 0.30%
    out = CexCexDetector().detect(ctx, "BTC", "USDT")
    assert out == []
    # exactly at floor → emitted (>=)
    ctx, cache = _econ_ctx(cfg, ("binance", "okx"))
    _cex(cache, "binance", "BTC", "99.9", "100.00")
    _cex(cache, "okx", "BTC", "100.35", "100.45")   # gross 0.35%
    out = CexCexDetector().detect(ctx, "BTC", "USDT")
    assert len(out) == 1 and out[0].gross_spread_pct == Decimal("0.35")


def test_cex_cex_floor_disabled_emits_below_floor():
    cfg = ScannerConfig()
    ctx, cache = _econ_ctx(cfg, ("binance", "okx"), economic_floor_enabled=False)
    _cex(cache, "binance", "BTC", "99.9", "100.00")
    _cex(cache, "okx", "BTC", "100.10", "100.20")   # gross 0.10% < floor
    out = CexCexDetector().detect(ctx, "BTC", "USDT")
    assert len(out) == 1                            # backward-compatible: no prune


def test_cex_cex_records_rejected_economic():
    cfg = ScannerConfig()
    ctx, cache = _econ_ctx(cfg, ("binance", "okx"))
    _cex(cache, "binance", "BTC", "99.9", "100.00")
    _cex(cache, "okx", "BTC", "100.10", "100.20")   # 0.10% < 0.35%
    det = CexCexDetector()
    det.detect(ctx, "BTC", "USDT")
    assert det.counters.rejected_economic == 1


# ── DEX↔DEX floor (dramatic reduction) ──────────────────────────────────────────────────
def _pool(cache, venue, network, base, rq):
    sym = CanonicalSymbol(base, "USDT", VenueType.DEX, network)
    cache.track(venue, sym)
    cache.upsert_book(OrderBook(venue, sym, [], [], pool_address=f"{venue}:{base}",
                                pool_fee_tier=Decimal("0.003"),
                                reserve_base=Decimal("100"), reserve_quote=Decimal(rq)))


def test_dex_dex_prunes_sub_roi_spread():
    cfg = ScannerConfig()
    ctx, cache = _econ_ctx(cfg, ("uni", "sushi"), venue_type=VenueType.DEX, network="ethereum")
    # spots 3000 vs 3003 → 0.10% < min_roi 0.15% → pruned (DEX floor ≈ min_roi).
    _pool(cache, "uni", "ethereum", "ETH", "300000")
    _pool(cache, "sushi", "ethereum", "ETH", "300300")
    det = DexDexDetector()
    assert det.detect(ctx, "ETH", "USDT") == []
    assert det.counters.rejected_economic == 1


def test_dex_dex_keeps_clearing_spread():
    cfg = ScannerConfig()
    ctx, cache = _econ_ctx(cfg, ("uni", "sushi"), venue_type=VenueType.DEX, network="ethereum")
    _pool(cache, "uni", "ethereum", "ETH", "300000")     # 3000
    _pool(cache, "sushi", "ethereum", "ETH", "303000")   # 3030 → 1% >> floor
    out = DexDexDetector().detect(ctx, "ETH", "USDT")
    assert len(out) == 1


# ── Cross-chain bridge floor ────────────────────────────────────────────────────────────
def test_cross_chain_bridge_floor_and_counter():
    cfg = ScannerConfig()
    ctx, cache = _econ_ctx(cfg, ("uni", "cake"), venue_type=VenueType.DEX)
    ctx.venues["uni"] = VenueInfo(id="uni", venue_type=VenueType.DEX, network="ethereum")
    ctx.venues["cake"] = VenueInfo(id="cake", venue_type=VenueType.DEX, network="bnb")
    _pool(cache, "uni", "ethereum", "ETH", "300000")     # 3000
    _pool(cache, "cake", "bnb", "ETH", "300300")         # 3003 → 0.10% < min_roi → pruned
    det = CrossChainDetector(BridgeRegistry())
    assert det.detect(ctx, "ETH", "USDT") == []
    assert det.counters.rejected_bridge >= 1

    ctx2, cache2 = _econ_ctx(cfg, ("uni", "cake"), venue_type=VenueType.DEX)
    ctx2.venues["uni"] = VenueInfo(id="uni", venue_type=VenueType.DEX, network="ethereum")
    ctx2.venues["cake"] = VenueInfo(id="cake", venue_type=VenueType.DEX, network="bnb")
    _pool(cache2, "uni", "ethereum", "ETH", "300000")    # 3000
    _pool(cache2, "cake", "bnb", "ETH", "306000")        # 3060 → 2% > floor
    out = CrossChainDetector(BridgeRegistry()).detect(ctx2, "ETH", "USDT")
    assert len(out) == 1


# ── Funding economic breakeven ──────────────────────────────────────────────────────────
def _funding_ctx(cfg, **over):
    return _econ_ctx(cfg, ("binance", "okx"), venue_type=VenueType.CEX,
                     cex_taker_rates={"binance": TAKER, "okx": TAKER}, **over)


def test_funding_breakeven_is_economically_derived():
    cfg = ScannerConfig()
    ctx, _ = _funding_ctx(cfg)
    # (min_net/size + 2×taker) / (hold_hours/(24×365)) = (0.005 + 0.002) / (168/8760).
    expected = (Decimal("5") / Decimal("1000") + 2 * TAKER) / (Decimal("168") / Decimal(24 * 365))
    assert ctx.funding_breakeven_annualized("binance", "okx") == expected
    assert expected > cfg.funding_min_annualized_spread   # stricter than the old fixed floor


def test_funding_prunes_below_breakeven_keeps_above():
    cfg = ScannerConfig()
    now = time.time()

    def run(low_rate, high_rate):
        ctx, cache = _funding_ctx(cfg)
        cache.upsert_funding(FundingRate("binance", "ETH", Decimal(low_rate), None, now + 3600, 8))
        cache.upsert_funding(FundingRate("okx", "ETH", Decimal(high_rate), None, now + 3600, 8))
        return FundingDetector().detect(ctx, "ETH", "USDT")

    # spread annualized ≈ 0.11 (< breakeven 0.365) → pruned
    assert run("0", "0.0001") == []
    # spread annualized ≈ 0.55 (> breakeven) → emitted
    out = run("0", "0.0005")
    assert len(out) == 1


# ── active-route exemption (§12.4) for a DEX route ──────────────────────────────────────
def test_active_route_exempt_from_dex_prune():
    cfg = ScannerConfig()
    ctx, cache = _econ_ctx(cfg, ("uni", "sushi"), venue_type=VenueType.DEX, network="ethereum")
    _pool(cache, "uni", "ethereum", "ETH", "300000")
    _pool(cache, "sushi", "ethereum", "ETH", "300300")   # 0.10% < floor
    assert DexDexDetector().detect(ctx, "ETH", "USDT") == []   # pruned when not active
    ctx.active_routes = frozenset({
        ("DEX_DEX", "ETH", "USDT", ("sushi", "uni"), "ethereum")})
    assert len(DexDexDetector().detect(ctx, "ETH", "USDT")) == 1   # exempt when active


# ── assembler still catches (safety net intact) ─────────────────────────────────────────
class _Ad(ExchangeAdapter):
    venue_type = VenueType.CEX
    network = None

    def __init__(self, vid):
        self.id = vid
        self.display_name = vid

    async def connect(self): ...
    async def disconnect(self): ...
    async def get_markets(self): return []
    async def subscribe_ticker(self, s): ...
    async def subscribe_order_book(self, s, d): ...
    async def get_funding_rate(self, a): return None
    async def get_pool_state(self, s): return None
    async def health_check(self): return True
    def get_status(self): return ExchangeStatus.ONLINE
    def taker_fee(self, s): return TAKER
    def withdrawal_fee_usd(self, a, n): return Decimal("1")
    def withdrawals_enabled(self, a, n): return True


class _Gas:
    async def gas_price_usd(self, n, u): return Decimal("0")


async def test_assembler_pregate_still_rejects_below_floor_candidate():
    # If a sub-fee candidate reaches the assembler (e.g. floor disabled at the detector),
    # the assembler pre-gate is still the final net and rejects it.
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = _health(cfg, ("binance", "okx"))
    adapters = {"binance": _Ad("binance"), "okx": _Ad("okx")}
    asm = SignalAssembler(cfg, cache, health, adapters, _Gas(), PriorityClassifier(cfg))
    cand = Candidate(arb_type=ArbitrageType.CEX_CEX, base_asset="ETH", quote_asset="USDT",
                     buy_leg=LegRef("binance", "CEX", Decimal("3000")),
                     sell_leg=LegRef("okx", "CEX", Decimal("3003")),
                     gross_spread_pct=Decimal("0.1"))   # below 0.35% floor
    res = await asm.assemble(cand)
    assert res.signal is None and res.pregate_hit is True


# ── backward compatibility of config ────────────────────────────────────────────────────
def test_config_defaults_present_and_backward_compatible():
    cfg = ScannerConfig()
    assert cfg.detector_economic_floor_enabled is True
    assert cfg.detector_cost_amortization_usd == 1_000_000.0
    assert cfg.detector_gas_estimate_usd == 1.0
    # untouched existing knobs
    assert cfg.min_roi_pct == 0.15 and cfg.funding_min_annualized_spread == 0.05


# ── safety: a candidate at the assembler publish threshold is NOT pruned ─────────────────
def test_detector_floor_never_prunes_a_publishable_spread():
    # The assembler publishes iff gross >= taker_floor + min_roi. A spread exactly there
    # must survive the detector (detector floor == assembler size-independent floor).
    cfg = ScannerConfig()
    ctx, cache = _econ_ctx(cfg, ("binance", "okx"))
    _cex(cache, "binance", "BTC", "99.9", "100.00")
    _cex(cache, "okx", "BTC", "100.35", "100.45")   # exactly floor+min_roi = 0.35%
    assert len(CexCexDetector().detect(ctx, "BTC", "USDT")) == 1
