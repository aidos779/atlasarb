"""Regressions for the two 'zero signals' root causes:

1. DEX pool books (reserves, no L2 levels) were dropped by the cache's
   is_empty_side() guard -> all DEX/CEX-DEX/DEX-DEX/cross-chain data lost.
2. Funding profit projected 1 day of carry against a full round-trip fee ->
   even a 40%+ annualized spread was rejected as UNPROFITABLE_AFTER_FEES.
"""
import time
from decimal import Decimal

import pytest

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, ExchangeStatus, VenueType
from src.domain.market import CanonicalSymbol, FundingRate, OrderBook
from src.domain.ports import ExchangeAdapter
from src.domain.signal import Candidate, FundingSnapshot, LegRef
from src.scanner.assembler import SignalAssembler
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.priority.scheduler import PriorityClassifier
from src.scanner.status.health_registry import HealthRegistry

DEX = CanonicalSymbol("ETH", "USDT", VenueType.DEX, "ethereum")


def _pool_book(venue):
    return OrderBook(venue, DEX, bids=[], asks=[], pool_address=f"{venue}:p",
                     pool_fee_tier=Decimal("0.003"),
                     reserve_base=Decimal("165"), reserve_quote=Decimal("500000"))


def test_pool_book_not_empty_side():
    assert _pool_book("uni").is_empty_side() is False
    # Genuinely empty (no levels, no reserves) still rejected.
    empty = OrderBook("x", DEX, bids=[], asks=[])
    assert empty.is_empty_side() is True


def test_pool_book_enters_cache():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    cache.track("uni", DEX)
    cache.upsert_book(_pool_book("uni"))
    got = cache.get_book("uni", "ETH/USDT")
    assert got is not None and got.reserve_base == Decimal("165")


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
    def taker_fee(self, s): return Decimal("0.001")
    def withdrawal_fee_usd(self, a, n): return Decimal("1")
    def withdrawals_enabled(self, a, n): return True


class _Gas:
    async def gas_price_usd(self, n, u): return Decimal("0")


async def _assemble_funding(annual_spread: str):
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    adapters = {"binance": _Ad("binance"), "okx": _Ad("okx")}
    for v in adapters:
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
    now = time.time()
    cache.upsert_funding(FundingRate("binance", "ETH", Decimal("0"), None, now + 3600, 8))
    cache.upsert_funding(FundingRate("okx", "ETH", Decimal("0"), None, now + 3600, 8))
    asm = SignalAssembler(cfg, cache, health, adapters, _Gas(), PriorityClassifier(cfg))
    snap = FundingSnapshot(received_at=now, current_rate=Decimal("0"),
                           has_predicted=False, has_next_time=True,
                           history=(Decimal("0"),))
    cand = Candidate(arb_type=ArbitrageType.FUNDING, base_asset="ETH", quote_asset="USDT",
                     buy_leg=LegRef("binance", "CEX", Decimal(1)),
                     sell_leg=LegRef("okx", "CEX", Decimal(1)),
                     gross_spread_pct=Decimal("0"),
                     funding_annualized_spread=Decimal(annual_spread),
                     funding_next_time=now + 3600,
                     funding_low=snap, funding_high=snap)
    return await asm.assemble(cand)


@pytest.mark.asyncio
async def test_high_funding_spread_publishes():
    res = await _assemble_funding("0.45")  # 45% annualized
    assert res.signal is not None, f"funding rejected: {res.reject_reason}"
    assert res.signal.net_profit_usd > 0


@pytest.mark.asyncio
async def test_tiny_funding_spread_still_rejected():
    res = await _assemble_funding("0.02")  # 2% annualized -> not worth the fees
    assert res.signal is None
