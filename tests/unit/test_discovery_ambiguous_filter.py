"""Ticker-collision denylist applied at discovery (issue #6).

"AI" is one token on Binance and an unrelated one on OKX, so any cross-venue spread on
it is fictional. The CexCex detector already rejects these at the identity level, but by
then the pair has consumed a scarce WS subscription slot on every venue. Filtering at
discovery keeps the collision out of the tracked set entirely — no subscription, no
cache entry, no per-tick heuristic.
"""
from __future__ import annotations

import pytest

from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.domain.market import CanonicalSymbol
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
        return None


def _cex(base: str) -> CanonicalSymbol:
    return CanonicalSymbol(base, "USDT", VenueType.CEX)


def _collector(cfg: ScannerConfig, adapter: _StubAdapter) -> MarketCollector:
    async def _force_expire(pair: str) -> int:
        return 0

    return MarketCollector(
        cfg, MarketStateCache(cfg), {"binance": adapter},
        PriorityClassifier(cfg), _force_expire,
    )


async def test_ambiguous_ticker_never_enters_tracked_set():
    adapter = _StubAdapter([_cex("AI"), _cex("BTC"), _cex("ETH")])
    collector = _collector(ScannerConfig(), adapter)   # default denylist seeds "AI"

    await collector.discover_once()

    assert collector.tracked_pairs() == {"BTC/USDT", "ETH/USDT"}
    # The decisive assertion: no scarce WS slot was spent on the collision.
    assert "AI/USDT" not in adapter.subscribed


async def test_empty_denylist_tracks_everything():
    adapter = _StubAdapter([_cex("AI"), _cex("BTC")])
    collector = _collector(ScannerConfig(ambiguous_tickers=frozenset()), adapter)

    await collector.discover_once()

    assert collector.tracked_pairs() == {"AI/USDT", "BTC/USDT"}


async def test_denylist_is_hot_reloadable():
    """A newly observed collision must be retirable without a redeploy."""
    adapter = _StubAdapter([_cex("AI"), _cex("NES")])
    collector = _collector(ScannerConfig(ambiguous_tickers=frozenset()), adapter)
    await collector.discover_once()
    assert "NES/USDT" in collector.tracked_pairs()

    collector.update_config(ScannerConfig(ambiguous_tickers=frozenset({"NES"})))
    await collector.discover_once()
    # Newly denied pair stops being rediscovered; it ages out via the delist grace path.
    assert "NES" not in {m.base_asset for m in
                         collector._filter_ambiguous("binance", [_cex("NES")])}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
