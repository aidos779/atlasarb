"""Regression tests for the hot-path memoizations.

These lock in the *invariants* the optimizations depend on, not the speedups:
a memo that outlives its tick, or a pair-key cache that disagrees with the
sorted-join it replaced, would silently corrupt detection or the funnel counters.
"""
from __future__ import annotations

from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook, PriceQuote
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.detectors.base import DetectionContext, VenueInfo
from src.scanner.monitoring.metrics import Metrics
from src.scanner.status.health_registry import HealthRegistry

SYM = CanonicalSymbol("ETH", "USDT", VenueType.CEX)
PAIR = "ETH/USDT"


def _ctx(cfg: ScannerConfig) -> tuple[DetectionContext, MarketStateCache]:
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    for v in ("binance", "okx"):
        health.register(v)
        for _ in range(3):
            health.record_success(v)
        cache.track(v, SYM)
    venues = {v: VenueInfo(id=v, venue_type=VenueType.CEX) for v in ("binance", "okx")}
    return DetectionContext(cache=cache, health=health, venues=venues), cache


def _seed(cache: MarketStateCache, venue: str, px: str) -> None:
    p = Decimal(px)
    cache.upsert_price(PriceQuote(venue, SYM, p, p * Decimal("1.001"), p))
    cache.upsert_book(OrderBook(venue, SYM, [BookLevel(p, Decimal("10"))],
                                [BookLevel(p * Decimal("1.001"), Decimal("10"))]))


def test_scan_is_shared_within_a_tick():
    """Two detectors asking for the same pair in one tick get one scan."""
    cfg = ScannerConfig()
    ctx, cache = _ctx(cfg)
    _seed(cache, "binance", "3000")
    _seed(cache, "okx", "3010")

    ctx.begin_tick()
    first = ctx.online_cex_prices(PAIR)
    second = ctx.online_cex_prices(PAIR)
    assert first is second, "second call in the same tick must reuse the memo"
    assert len(first) == 2


def test_new_tick_sees_updated_cache():
    """The memo must never outlive its tick — a later tick reads fresh state."""
    cfg = ScannerConfig()
    ctx, cache = _ctx(cfg)
    _seed(cache, "binance", "3000")

    ctx.begin_tick()
    assert len(ctx.online_cex_prices(PAIR)) == 1

    _seed(cache, "okx", "3010")
    # Same tick: still the memoized single-venue view.
    assert len(ctx.online_cex_prices(PAIR)) == 1
    # New tick: the second venue is visible.
    ctx.begin_tick()
    assert len(ctx.online_cex_prices(PAIR)) == 2


def test_memo_is_inert_without_begin_tick():
    """A context driven directly (unit tests, ad-hoc callers) never memoizes."""
    cfg = ScannerConfig()
    ctx, cache = _ctx(cfg)
    _seed(cache, "binance", "3000")

    assert len(ctx.online_cex_prices(PAIR)) == 1
    _seed(cache, "okx", "3010")
    assert len(ctx.online_cex_prices(PAIR)) == 2, "no begin_tick -> no caching"


def test_dex_and_cex_memos_are_independent():
    cfg = ScannerConfig()
    ctx, cache = _ctx(cfg)
    _seed(cache, "binance", "3000")
    ctx.begin_tick()
    assert len(ctx.online_cex_prices(PAIR)) == 1
    assert ctx.online_dex_books(PAIR) == []


def test_pair_key_matches_sorted_join():
    """The memoized key must equal the sorted-join it replaced, both orderings."""
    m = Metrics()
    for a, b in (("okx", "binance"), ("binance", "okx"), ("a", "b"), ("z", "a")):
        assert m._pair_key(a, b) == "↔".join(sorted((a, b)))
    # Order-independence is what makes the funnel counters aggregate correctly.
    assert m._pair_key("okx", "binance") == m._pair_key("binance", "okx")


def test_record_pair_checked_counts_every_combination():
    m = Metrics()
    venues = ["binance", "okx", "bybit"]
    for _ in range(3):
        m.record_pair_checked(venues)
    snap = m.venue_pair_snapshot()
    assert set(snap) == {"binance↔okx", "binance↔bybit", "bybit↔okx"}
    assert all(c["checked"] == 3 for c in snap.values())


def test_record_pair_checked_handles_changing_rosters():
    """A different online-venue roster must not reuse the previous roster's counters."""
    m = Metrics()
    m.record_pair_checked(["binance", "okx"])
    m.record_pair_checked(["binance", "bybit"])
    snap = m.venue_pair_snapshot()
    assert snap["binance↔okx"]["checked"] == 1
    assert snap["binance↔bybit"]["checked"] == 1
