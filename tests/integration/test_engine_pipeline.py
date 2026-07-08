"""End-to-end scanning engine test with synthetic adapters (no live network)."""
from decimal import Decimal

import pytest

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ExchangeStatus, VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook, PriceQuote
from src.domain.ports import ExchangeAdapter
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.engine import ScanningEngine
from src.scanner.status.health_registry import HealthRegistry

pytestmark = pytest.mark.asyncio

SYM = CanonicalSymbol("ETH", "USDT", VenueType.CEX)


class FakeCex(ExchangeAdapter):
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


class Queue:
    def __init__(self):
        self.msgs = []

    async def publish(self, signal, event):
        self.msgs.append((event, signal))


class History:
    async def archive(self, signal): ...


class Gas:
    async def gas_price_usd(self, n, u): return Decimal("0")


async def test_cex_cex_signal_end_to_end():
    cfg = ScannerConfig()
    adapters = {"binance": FakeCex("binance"), "okx": FakeCex("okx")}
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    for v in adapters:
        health.register(v)
        for _ in range(3):
            health.record_success(v)
    queue = Queue()
    engine = ScanningEngine(cfg, adapters, queue, History(), Gas(), {"ETH"},
                            cache=cache, health=health)
    cache.track("binance", SYM)
    cache.track("okx", SYM)
    for _ in range(3):
        cache.upsert_price(PriceQuote("binance", SYM, Decimal("3000"), Decimal("3001"),
                                      Decimal("3000")))
        cache.upsert_price(PriceQuote("okx", SYM, Decimal("3050"), Decimal("3051"),
                                      Decimal("3050")))
    cache.upsert_book(OrderBook("binance", SYM, [BookLevel(Decimal("3000"), Decimal("100"))],
                                [BookLevel(Decimal("3001"), Decimal("100"))]))
    cache.upsert_book(OrderBook("okx", SYM, [BookLevel(Decimal("3050"), Decimal("100"))],
                                [BookLevel(Decimal("3051"), Decimal("100"))]))

    await engine._process_symbol("ETH", "USDT")

    assert len(queue.msgs) == 1
    event, signal = queue.msgs[0]
    assert event == "new"
    assert signal.buy_exchange == "binance"
    assert signal.sell_exchange == "okx"
    assert signal.net_profit_pct > 0


async def test_offline_venue_produces_no_signal():
    cfg = ScannerConfig()
    adapters = {"binance": FakeCex("binance"), "okx": FakeCex("okx")}
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    for v in adapters:
        health.register(v)
    for _ in range(3):
        health.record_success("binance")
    health.mark_offline("okx")  # okx offline
    queue = Queue()
    engine = ScanningEngine(cfg, adapters, queue, History(), Gas(), {"ETH"},
                            cache=cache, health=health)
    cache.track("binance", SYM)
    cache.track("okx", SYM)
    for _ in range(3):
        cache.upsert_price(PriceQuote("binance", SYM, Decimal("3000"), Decimal("3001"),
                                      Decimal("3000")))
        cache.upsert_price(PriceQuote("okx", SYM, Decimal("3050"), Decimal("3051"),
                                      Decimal("3050")))
    cache.upsert_book(OrderBook("binance", SYM, [BookLevel(Decimal("3000"), Decimal("100"))],
                                [BookLevel(Decimal("3001"), Decimal("100"))]))
    cache.upsert_book(OrderBook("okx", SYM, [BookLevel(Decimal("3050"), Decimal("100"))],
                                [BookLevel(Decimal("3051"), Decimal("100"))]))

    await engine._process_symbol("ETH", "USDT")
    assert len(queue.msgs) == 0  # §14.3 no signal for offline venue
