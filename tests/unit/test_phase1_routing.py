"""Phase 1 — typed event routing, funding event routing, and the CrossChain
double-direction fix. These lock in that:

  * a funding write drives the funding detector directly (not only via CEX book noise);
  * a cache write only runs the detectors whose inputs changed (typed routing), and
    groups accumulated while a symbol is queued are merged (no dropped DEX detection);
  * CrossChain evaluates each cross-network pool pair once (no phantom spread reject).
"""
from __future__ import annotations

from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ExchangeStatus, VenueType
from src.domain.market import CanonicalSymbol, FundingRate, OrderBook
from src.domain.ports import ExchangeAdapter
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.detectors.base import DetectionContext, VenueInfo
from src.scanner.detectors.bridges import BridgeRegistry
from src.scanner.detectors.cross_chain import CrossChainDetector
from src.scanner.engine import (
    _GROUP_CEX,
    _GROUP_DEX,
    _GROUP_FUNDING,
    ScanningEngine,
)
from src.scanner.status.health_registry import HealthRegistry


# ── fakes ─────────────────────────────────────────────────────────────────────────────
class _Cex(ExchangeAdapter):
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
    def taker_fee(self, s): return Decimal("0.001")
    def withdrawal_fee_usd(self, a, n): return Decimal("1")
    def withdrawals_enabled(self, a, n): return True


class _Dex(_Cex):
    venue_type = VenueType.DEX
    network = "ethereum"


class _Queue:
    def __init__(self): self.msgs = []
    async def publish(self, signal, event): self.msgs.append((event, signal))


class _History:
    async def archive(self, signal): ...


class _Gas:
    async def gas_price_usd(self, n, u): return Decimal("0")


def _engine():
    cfg = ScannerConfig()
    adapters = {"binance": _Cex("binance"), "uni": _Dex("uni")}
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    for v in adapters:
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
    eng = ScanningEngine(cfg, adapters, _Queue(), _History(), _Gas(), {"ETH"},
                         cache=cache, health=health)
    return eng, cache


def _checked(eng) -> dict[str, int]:
    return {d.arb_type: d.counters.opportunities_checked for d in eng._detectors}


# ── typed routing: only relevant detectors run ─────────────────────────────────────────
async def test_cex_group_runs_only_cex_detectors():
    eng, _ = _engine()
    await eng._process_symbol("ETH", "USDT", {_GROUP_CEX})
    checked = _checked(eng)
    assert checked["CEX_CEX"] == 1
    assert checked["CEX_DEX"] == 1          # reads the CEX leg too
    assert checked["DEX_DEX"] == 0
    assert checked["CROSS_CHAIN"] == 0
    assert checked["FUNDING"] == 0


async def test_dex_group_runs_only_dex_detectors():
    eng, _ = _engine()
    await eng._process_symbol("ETH", "USDT", {_GROUP_DEX})
    checked = _checked(eng)
    assert checked["CEX_CEX"] == 0
    assert checked["CEX_DEX"] == 1          # reads the DEX leg too
    assert checked["DEX_DEX"] == 1
    assert checked["CROSS_CHAIN"] == 1
    assert checked["FUNDING"] == 0


async def test_funding_group_runs_only_funding_detector():
    eng, _ = _engine()
    await eng._process_symbol("ETH", "USDT", {_GROUP_FUNDING})
    checked = _checked(eng)
    assert checked["FUNDING"] == 1
    assert sum(v for k, v in checked.items() if k != "FUNDING") == 0


async def test_default_groups_run_every_detector():
    # A direct/reconciliation call (no groups) must still run all five (safety net).
    eng, _ = _engine()
    await eng._process_symbol("ETH", "USDT")
    assert all(v == 1 for v in _checked(eng).values())


# ── event → group mapping + coalesce merge ─────────────────────────────────────────────
def test_cex_write_tags_cex_group_and_enqueues():
    eng, _ = _engine()
    eng._on_cache_write("ETH", "USDT", "binance")
    assert eng._pending_groups[("ETH", "USDT")] == {_GROUP_CEX}
    assert eng._event_queue.pending() == 1


def test_dex_write_tags_dex_group():
    eng, _ = _engine()
    eng._on_cache_write("ETH", "USDT", "uni")
    assert eng._pending_groups[("ETH", "USDT")] == {_GROUP_DEX}


def test_funding_write_tags_funding_group_on_usdt_key():
    eng, _ = _engine()
    eng._on_funding_write("ETH", "binance")
    assert eng._pending_groups[("ETH", "USDT")] == {_GROUP_FUNDING}
    assert eng._event_queue.pending() == 1


def test_groups_merge_while_symbol_is_queued():
    # CEX then DEX then funding for the same symbol before it is processed → the single
    # queued item must carry all three groups (queue coalesces, groups must not be lost).
    eng, _ = _engine()
    eng._on_cache_write("ETH", "USDT", "binance")   # cex
    eng._on_cache_write("ETH", "USDT", "uni")       # dex
    eng._on_funding_write("ETH", "binance")         # funding
    assert eng._pending_groups[("ETH", "USDT")] == {_GROUP_CEX, _GROUP_DEX, _GROUP_FUNDING}
    assert eng._event_queue.pending() == 1          # still one coalesced item


# ── funding event routing at the cache boundary ────────────────────────────────────────
def test_upsert_funding_fires_subscribe_funding():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    seen: list[tuple[str, str]] = []
    cache.subscribe_funding(lambda base, venue: seen.append((base, venue)))
    cache.upsert_funding(FundingRate("binance", "ETH", Decimal("0.0001"), None,
                                     1_000_000_000, 8))
    assert seen == [("ETH", "binance")]


def test_upsert_price_does_not_fire_funding_channel():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    fired = []
    cache.subscribe_funding(lambda base, venue: fired.append((base, venue)))
    sym = CanonicalSymbol("ETH", "USDT", VenueType.CEX)
    cache.track("binance", sym)
    from src.domain.market import PriceQuote
    cache.upsert_price(PriceQuote("binance", sym, Decimal("3000"), Decimal("3001"),
                                  Decimal("3000")))
    assert fired == []


# ── CrossChain double-direction fix ────────────────────────────────────────────────────
def _xchain_ctx(cfg):
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    venues = {
        "uni": VenueInfo(id="uni", venue_type=VenueType.DEX, network="ethereum"),
        "cake": VenueInfo(id="cake", venue_type=VenueType.DEX, network="bnb"),
    }
    for v in venues:
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
    ctx = DetectionContext(cache=cache, health=health, venues=venues,
                           verified_tokens={"ETH"})
    return ctx, cache


def _pool(cache, venue, network, reserve_base, reserve_quote):
    sym = CanonicalSymbol("ETH", "USDT", VenueType.DEX, network)
    cache.track(venue, sym)
    cache.upsert_book(OrderBook(venue, sym, bids=[], asks=[], pool_address=f"{venue}:p",
                                pool_fee_tier=Decimal("0.003"),
                                reserve_base=Decimal(reserve_base),
                                reserve_quote=Decimal(reserve_quote)))


def test_cross_chain_emits_one_candidate_without_phantom_reject():
    cfg = ScannerConfig()
    ctx, cache = _xchain_ctx(cfg)
    _pool(cache, "uni", "ethereum", "100", "300000")   # spot 3000 (cheaper → buy)
    _pool(cache, "cake", "bnb", "100", "306000")        # spot 3060 (dearer → sell)
    det = CrossChainDetector(BridgeRegistry())
    out = det.detect(ctx, "ETH", "USDT")
    assert len(out) == 1
    assert out[0].buy_leg.venue == "uni" and out[0].sell_leg.venue == "cake"
    assert out[0].gross_spread_pct > 0
    # The mirror direction is no longer walked, so no phantom spread reject is counted.
    assert det.counters.rejected_by_spread == 0


def test_cross_chain_same_chain_pair_is_skipped():
    cfg = ScannerConfig()
    ctx, cache = _xchain_ctx(cfg)
    # Both ethereum → same chain, handled by DEX-DEX, not cross-chain.
    ctx.venues["cake"] = VenueInfo(id="cake", venue_type=VenueType.DEX, network="ethereum")
    _pool(cache, "uni", "ethereum", "100", "300000")
    _pool(cache, "cake", "ethereum", "100", "306000")
    det = CrossChainDetector(BridgeRegistry())
    out = det.detect(ctx, "ETH", "USDT")
    assert out == []
