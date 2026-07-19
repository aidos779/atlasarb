"""USDT is the only supported quote asset (§3.6, R-QUOTE-1).

USDC was removed from the scanner entirely. Every USDC market duplicated its USDT twin:
it doubled the WS subscription and cache footprint, and each opportunity surfaced twice —
once per quote — so users received two alerts differing only in the quote leg.

These tests pin the three layers that enforce it: the ingest predicate, the discovery
filter (no subscription, no tracked pair), and the cache (no book, no snapshot).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.config.scanner_config import ScannerConfig
from src.domain.enums import QuoteAsset, VenueType
from src.domain.market import (
    BookLevel,
    CanonicalSymbol,
    OrderBook,
    PriceQuote,
    is_supported_pair,
    is_supported_quote,
)
from src.scanner.adapters.dex.pool_registry import QUOTE_TOKENS, VERIFIED_TOKENS
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.collectors.market_collector import MarketCollector
from src.scanner.priority.scheduler import PriorityClassifier


class _StubAdapter:
    venue_type = VenueType.CEX

    def __init__(self, markets: list[CanonicalSymbol]):
        self._markets = markets
        self.subscribed: list[str] = []

    async def get_markets(self) -> list[CanonicalSymbol]:
        return list(self._markets)

    async def subscribe_ticker(self, symbols) -> None:
        self.subscribed.extend(s.pair for s in symbols)

    async def subscribe_order_book(self, symbols, depth: int = 20) -> None:
        self.subscribed.extend(s.pair for s in symbols)


def _sym(base: str, quote: str) -> CanonicalSymbol:
    return CanonicalSymbol(base, quote, VenueType.CEX)


def _collector(adapter: _StubAdapter, cfg: ScannerConfig | None = None) -> MarketCollector:
    cfg = cfg or ScannerConfig(ambiguous_tickers=frozenset())

    async def _force_expire(pair: str) -> int:
        return 0

    return MarketCollector(
        cfg, MarketStateCache(cfg), {"binance": adapter},
        PriorityClassifier(cfg), _force_expire,
    )


# ── ingest predicate ──
def test_usdt_is_the_only_quote_asset():
    assert [q.value for q in QuoteAsset] == ["USDT"]
    assert not hasattr(QuoteAsset, "USDC")


@pytest.mark.parametrize("pair", ["BTC/USDC", "BTC-USDC", "btc/usdc", "ETH_USDC",
                                  "WETH/USDC", "BTC/BUSD", "BTC/DAI"])
def test_non_usdt_pairs_are_rejected(pair):
    assert not is_supported_pair(pair)


@pytest.mark.parametrize("pair", ["BTC/USDT", "BTC-USDT", "btc/usdt", "ETH_USDT"])
def test_usdt_pairs_are_accepted(pair):
    assert is_supported_pair(pair)


def test_usdc_quote_asset_is_not_supported():
    assert is_supported_quote("USDT")
    assert not is_supported_quote("USDC")


def test_usdc_base_asset_is_not_mistaken_for_a_usdc_quote():
    """USDC-as-base must not be confused with USDC-as-quote by a naive substring check."""
    assert is_supported_pair("USDC/USDT")


# ── discovery ──
async def test_usdc_markets_never_enter_the_tracked_set():
    adapter = _StubAdapter([_sym("BTC", "USDT"), _sym("BTC", "USDC"),
                            _sym("ETH", "USDT"), _sym("ETH", "USDC")])
    collector = _collector(adapter)

    await collector.discover_once()

    assert collector.tracked_pairs() == {"BTC/USDT", "ETH/USDT"}


async def test_no_usdc_order_book_is_ever_subscribed():
    """The decisive assertion: a USDC pair must not consume a scarce WS slot."""
    adapter = _StubAdapter([_sym("BTC", "USDT"), _sym("BTC", "USDC")])
    collector = _collector(adapter)

    await collector.discover_once()

    assert not any("USDC" in pair for pair in adapter.subscribed)
    assert "BTC/USDT" in adapter.subscribed


async def test_usdt_discovery_still_works_when_venue_lists_only_usdt():
    adapter = _StubAdapter([_sym("BTC", "USDT"), _sym("SOL", "USDT")])
    collector = _collector(adapter)

    await collector.discover_once()

    assert collector.tracked_pairs() == {"BTC/USDT", "SOL/USDT"}


async def test_venue_listing_only_usdc_yields_no_pairs():
    adapter = _StubAdapter([_sym("BTC", "USDC"), _sym("ETH", "USDC")])
    collector = _collector(adapter)

    await collector.discover_once()

    assert collector.tracked_pairs() == set()
    assert adapter.subscribed == []


# ── cache ──
def _quote(sym: CanonicalSymbol) -> PriceQuote:
    return PriceQuote(venue="binance", symbol=sym, bid=Decimal(100),
                      ask=Decimal(101), last=Decimal(100))


def _book(sym: CanonicalSymbol) -> OrderBook:
    return OrderBook(venue="binance", symbol=sym,
                     bids=[BookLevel(Decimal(100), Decimal(10))],
                     asks=[BookLevel(Decimal(101), Decimal(10))])


def test_cache_refuses_to_track_a_usdc_symbol():
    cache = MarketStateCache(ScannerConfig())
    usdc, usdt = _sym("BTC", "USDC"), _sym("BTC", "USDT")

    cache.track("binance", usdc)
    cache.track("binance", usdt)

    assert not cache.is_tracked("binance", "BTC/USDC")
    assert cache.is_tracked("binance", "BTC/USDT")


def test_no_usdc_book_or_snapshot_is_stored():
    """Untracked pairs are dropped by upsert_*, so no USDC state occupies memory."""
    cache = MarketStateCache(ScannerConfig())
    usdc = _sym("BTC", "USDC")
    cache.track("binance", usdc)

    cache.upsert_price(_quote(usdc))
    cache.upsert_book(_book(usdc))

    assert cache.get_price("binance", "BTC/USDC") is None
    assert cache.get_book("binance", "BTC/USDC") is None
    assert cache.tracked_pairs() == set()


def test_usdt_books_and_snapshots_are_still_stored():
    """Guard against the filter over-reaching and starving the supported quote."""
    cfg = ScannerConfig(warmup_samples=1)
    cache = MarketStateCache(cfg)
    usdt = _sym("BTC", "USDT")
    cache.track("binance", usdt)

    cache.upsert_price(_quote(usdt))
    cache.upsert_book(_book(usdt))

    assert cache.get_price("binance", "BTC/USDT") is not None
    assert cache.get_book("binance", "BTC/USDT") is not None


# ── DEX registry ──
def test_dex_quote_tokens_are_usdt_only():
    for network, quotes in QUOTE_TOKENS.items():
        assert [q.symbol for q in quotes] == ["USDT"], network


def test_usdc_is_not_a_verified_dex_base_asset():
    assert "USDC" not in VERIFIED_TOKENS
    assert "USDT" in VERIFIED_TOKENS


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
