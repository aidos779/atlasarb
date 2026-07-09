"""End-to-end proof that every arbitrage type reaches the notification stage.

Drives the *real* engine pipeline (detector → assembler → profit engine → validator →
lifecycle → notification queue) with synthetic-but-valid market state injected into the
real cache. CEX_CEX is already covered by test_engine_pipeline; this file adds the four
harder types (CEX_DEX, DEX_DEX, CROSS_CHAIN, FUNDING) so a regression that silently kills
any detector's publish path is caught. Thresholds are loosened only to make the emission
deterministic; the code path exercised is production.
"""
from decimal import Decimal

import pytest

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ExchangeStatus, VenueType
from src.domain.market import (
    BookLevel,
    CanonicalSymbol,
    FundingRate,
    OrderBook,
    PriceQuote,
)
from src.domain.ports import ExchangeAdapter
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.engine import ScanningEngine
from src.scanner.status.health_registry import HealthRegistry

pytestmark = pytest.mark.asyncio


def _permissive_cfg() -> ScannerConfig:
    # Loosen the profit / liquidity / confidence floors so a valid opportunity is
    # published deterministically; every pipeline STAGE still runs unchanged.
    return ScannerConfig(
        min_net_profit_usd=0.5,
        min_roi_pct=0.0,
        confidence_threshold=0.0,
        liquidity_score_floor=0.0,
        min_liquidity_cex_usd=100.0,
        min_liquidity_dex_usd=100.0,
        min_tradeable_size_usd=10.0,
    )


class _FakeBase(ExchangeAdapter):
    def __init__(self, vid, network=None):
        self.id = vid
        self.display_name = vid
        self.network = network

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


class FakeCex(_FakeBase):
    venue_type = VenueType.CEX


class FakeDex(_FakeBase):
    venue_type = VenueType.DEX

    def taker_fee(self, s): return Decimal("0.003")
    def withdrawal_fee_usd(self, a, n): return Decimal("0")


class Queue:
    def __init__(self):
        self.msgs = []

    async def publish(self, signal, event):
        self.msgs.append((event, signal))


class History:
    async def archive(self, signal): ...


class Gas:
    async def gas_price_usd(self, n, u): return Decimal("0.5")


def _online(health, *venues):
    for v in venues:
        health.register(v)
        for _ in range(3):
            health.record_success(v)


def _dex_book(venue, network, base, quote, reserve_base, reserve_quote, pool):
    sym = CanonicalSymbol(base, quote, VenueType.DEX, network)
    return OrderBook(
        venue, sym, bids=[], asks=[],
        pool_address=pool, pool_fee_tier=Decimal("0.003"),
        reserve_base=Decimal(str(reserve_base)), reserve_quote=Decimal(str(reserve_quote)),
    )


def _cex_state(cache, venue, base, quote, bid, ask):
    sym = CanonicalSymbol(base, quote, VenueType.CEX)
    cache.track(venue, sym)
    for _ in range(3):
        cache.upsert_price(PriceQuote(venue, sym, Decimal(str(bid)), Decimal(str(ask)),
                                      Decimal(str(bid))))
    cache.upsert_book(OrderBook(venue, sym,
                                [BookLevel(Decimal(str(bid)), Decimal("1000000"))],
                                [BookLevel(Decimal(str(ask)), Decimal("1000000"))]))


async def test_cex_dex_signal_end_to_end():
    cfg = _permissive_cfg()
    adapters = {"binance": FakeCex("binance"),
                "uniswap_ethereum": FakeDex("uniswap_ethereum", network="ethereum")}
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    _online(health, *adapters)
    queue = Queue()
    engine = ScanningEngine(cfg, adapters, queue, History(), Gas(), {"ETH"},
                            cache=cache, health=health)
    # CEX ETH ask ~3000; DEX pool spot ~3120 (4% higher) -> buy CEX, sell DEX.
    _cex_state(cache, "binance", "ETH", "USDT", 2999, 3000)
    sym = CanonicalSymbol("ETH", "USDT", VenueType.DEX, "ethereum")
    cache.track("uniswap_ethereum", sym)
    for _ in range(3):
        cache.upsert_book(_dex_book("uniswap_ethereum", "ethereum", "ETH", "USDT",
                                    10000, 31_200_000, "0xpoolA"))

    await engine._process_symbol("ETH", "USDT")

    published = [s for _, s in queue.msgs if s.arb_type.value == "CEX_DEX"]
    assert published, f"no CEX_DEX signal; msgs={[s.arb_type.value for _, s in queue.msgs]}"
    assert published[0].net_profit_usd > 0


async def test_dex_dex_signal_end_to_end():
    cfg = _permissive_cfg()
    adapters = {"uniswap_ethereum": FakeDex("uniswap_ethereum", network="ethereum"),
                "sushiswap_ethereum": FakeDex("sushiswap_ethereum", network="ethereum")}
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    _online(health, *adapters)
    queue = Queue()
    engine = ScanningEngine(cfg, adapters, queue, History(), Gas(), {"ETH"},
                            cache=cache, health=health)
    sym_u = CanonicalSymbol("ETH", "USDT", VenueType.DEX, "ethereum")
    cache.track("uniswap_ethereum", sym_u)
    cache.track("sushiswap_ethereum", sym_u)
    for _ in range(3):
        # Uniswap spot ~3000, Sushi spot ~3150 (5% gap) -> buy uni, sell sushi.
        cache.upsert_book(_dex_book("uniswap_ethereum", "ethereum", "ETH", "USDT",
                                    10000, 30_000_000, "0xpoolU"))
        cache.upsert_book(_dex_book("sushiswap_ethereum", "ethereum", "ETH", "USDT",
                                    10000, 31_500_000, "0xpoolS"))

    await engine._process_symbol("ETH", "USDT")

    published = [s for _, s in queue.msgs if s.arb_type.value == "DEX_DEX"]
    assert published, f"no DEX_DEX signal; msgs={[s.arb_type.value for _, s in queue.msgs]}"
    assert published[0].atomic_execution is True
    assert published[0].net_profit_usd > 0


async def test_cross_chain_signal_end_to_end():
    cfg = _permissive_cfg()
    adapters = {"uniswap_ethereum": FakeDex("uniswap_ethereum", network="ethereum"),
                "uniswap_arbitrum": FakeDex("uniswap_arbitrum", network="arbitrum")}
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    _online(health, *adapters)
    queue = Queue()
    engine = ScanningEngine(cfg, adapters, queue, History(), Gas(), {"ETH"},
                            cache=cache, health=health)
    sym_eth = CanonicalSymbol("ETH", "USDT", VenueType.DEX, "ethereum")
    sym_arb = CanonicalSymbol("ETH", "USDT", VenueType.DEX, "arbitrum")
    cache.track("uniswap_ethereum", sym_eth)
    cache.track("uniswap_arbitrum", sym_arb)
    for _ in range(3):
        # ethereum spot ~3000, arbitrum spot ~3300 (10%) -> bridge route exists (Across).
        cache.upsert_book(_dex_book("uniswap_ethereum", "ethereum", "ETH", "USDT",
                                    10000, 30_000_000, "0xpoolE"))
        cache.upsert_book(_dex_book("uniswap_arbitrum", "arbitrum", "ETH", "USDT",
                                    10000, 33_000_000, "0xpoolR"))

    await engine._process_symbol("ETH", "USDT")

    published = [s for _, s in queue.msgs if s.arb_type.value == "CROSS_CHAIN"]
    assert published, f"no CROSS_CHAIN signal; msgs={[s.arb_type.value for _, s in queue.msgs]}"
    assert published[0].bridge_name is not None
    assert published[0].net_profit_usd > 0


async def test_funding_signal_end_to_end():
    cfg = _permissive_cfg()
    adapters = {"binance": FakeCex("binance"), "okx": FakeCex("okx")}
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    _online(health, *adapters)
    queue = Queue()
    engine = ScanningEngine(cfg, adapters, queue, History(), Gas(), {"ETH"},
                            cache=cache, health=health, perp_assets={"ETH"})
    import time
    nxt = time.time() + 3600
    # Large funding differential (long binance @ -0.05%, short okx @ +0.05% per 8h).
    cache.upsert_funding(FundingRate("binance", "ETH", Decimal("-0.0005"), None, nxt, 8))
    cache.upsert_funding(FundingRate("okx", "ETH", Decimal("0.0005"), None, nxt, 8))

    await engine._process_symbol("ETH", "USDT")

    published = [s for _, s in queue.msgs if s.arb_type.value == "FUNDING"]
    assert published, f"no FUNDING signal; msgs={[s.arb_type.value for _, s in queue.msgs]}"
    assert published[0].net_profit_usd > 0
