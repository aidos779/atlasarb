"""Identity-level ticker-collision guard (audit item 11).

A ticker known to map to different tokens across venues (e.g. "AI") is skipped up front,
so it never re-trips the `cex_cex_spread_implausible` heuristic every tick. A normal,
non-ambiguous pair with the same plausible spread still produces a candidate.
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


def _ctx(cfg: ScannerConfig, ambiguous: frozenset[str]):
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    venues = {}
    for v in ("binance", "okx"):
        health.register(v)
        for _ in range(3):
            health.record_success(v)
        venues[v] = VenueInfo(id=v, venue_type=VenueType.CEX)
    ctx = DetectionContext(
        cache=cache, health=health, venues=venues,
        max_plausible_cex_spread_pct=Decimal(str(cfg.max_plausible_cex_spread_pct)),
        ambiguous_tickers=ambiguous,
    )
    return ctx, cache


def _cex(cache, venue, base, bid, ask):
    sym = CanonicalSymbol(base, "USDT", VenueType.CEX)
    cache.track(venue, sym)
    for _ in range(3):
        cache.upsert_price(PriceQuote(venue, sym, Decimal(bid), Decimal(ask), Decimal(bid)))
    cache.upsert_book(OrderBook(venue, sym, [BookLevel(Decimal(bid), Decimal("1000000"))],
                                [BookLevel(Decimal(ask), Decimal("1000000"))]))


def test_ambiguous_ticker_is_skipped_before_heuristic():
    cfg = ScannerConfig()
    ctx, cache = _ctx(cfg, ambiguous=frozenset({"AI"}))
    # A plausible ~1% spread that WOULD normally emit a candidate.
    _cex(cache, "binance", "AI", "0.2676", "0.2677")
    _cex(cache, "okx", "AI", "0.2704", "0.2705")
    assert CexCexDetector().detect(ctx, "AI", "USDT") == []


def test_non_ambiguous_ticker_still_emits():
    cfg = ScannerConfig()
    ctx, cache = _ctx(cfg, ambiguous=frozenset({"AI"}))
    _cex(cache, "binance", "NES", "0.2676", "0.2677")
    _cex(cache, "okx", "NES", "0.2704", "0.2705")
    out = CexCexDetector().detect(ctx, "NES", "USDT")
    assert len(out) == 1


def test_default_config_seeds_ai():
    assert "AI" in ScannerConfig().ambiguous_tickers
