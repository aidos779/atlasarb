"""Ticker-collision guard for CEX↔CEX (regression for the phantom 28% AI/USDT signal).

A double-digit spot spread on the same symbol across two exchanges is almost always two
DIFFERENT tokens sharing a ticker (or a stale tick), not a real arbitrage. The detector
must drop such candidates before the profit pipeline, while still emitting a normal,
plausible spread.
"""
from __future__ import annotations

from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook, PriceQuote
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.detectors.base import DetectionContext, VenueInfo
from src.scanner.detectors.cex_cex import CexCexDetector
from src.scanner.status.health_registry import HealthRegistry


def _ctx(cfg: ScannerConfig) -> DetectionContext:
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    venues = {}
    for v in ("binance", "okx"):
        health.register(v)
        for _ in range(3):
            health.record_success(v)
        venues[v] = VenueInfo(id=v, venue_type=VenueType.CEX)
    return DetectionContext(
        cache=cache, health=health, venues=venues,
        max_plausible_cex_spread_pct=Decimal(str(cfg.max_plausible_cex_spread_pct)),
    ), cache


def _cex(cache: MarketStateCache, venue: str, base: str, bid: str, ask: str) -> None:
    sym = CanonicalSymbol(base, "USDT", VenueType.CEX)
    cache.track(venue, sym)
    for _ in range(3):
        cache.upsert_price(PriceQuote(venue, sym, Decimal(bid), Decimal(ask), Decimal(bid)))
    cache.upsert_book(OrderBook(venue, sym,
                                [BookLevel(Decimal(bid), Decimal("1000000"))],
                                [BookLevel(Decimal(ask), Decimal("1000000"))]))


def test_collision_spread_is_dropped():
    cfg = ScannerConfig()  # ceiling 10%
    ctx, cache = _ctx(cfg)
    # AI: binance 0.0202 vs okx 0.026 -> ~28% "spread" = ticker collision.
    _cex(cache, "binance", "AI", "0.0201", "0.0202")
    _cex(cache, "okx", "AI", "0.0260", "0.0261")
    assert CexCexDetector().detect(ctx, "AI", "USDT") == []


def test_plausible_spread_still_emitted():
    cfg = ScannerConfig()
    ctx, cache = _ctx(cfg)
    # NES: a real ~1% dislocation must still produce a candidate.
    _cex(cache, "binance", "NES", "0.26762", "0.26763")
    _cex(cache, "okx", "NES", "0.2704", "0.2705")
    out = CexCexDetector().detect(ctx, "NES", "USDT")
    assert len(out) == 1
    assert out[0].buy_leg.venue == "binance"
    assert out[0].sell_leg.venue == "okx"


def test_ceiling_zero_disables_guard():
    cfg = ScannerConfig(max_plausible_cex_spread_pct=0.0)
    ctx, cache = _ctx(cfg)
    _cex(cache, "binance", "AI", "0.0201", "0.0202")
    _cex(cache, "okx", "AI", "0.0260", "0.0261")
    # Guard disabled -> the (phantom) candidate is emitted again.
    assert len(CexCexDetector().detect(ctx, "AI", "USDT")) == 1
