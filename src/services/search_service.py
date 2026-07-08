"""Search service (PRD §11) — coins & exchanges only, aliased, rate-limited.

Case/punctuation-insensitive with common aliasing (BR-SEARCH-5). Results count live
active signals per match. 20/min/user rate limit (BR-SEARCH-2).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.services.rate_limiter import SlidingWindowLimiter
from src.services.signal_registry import SignalRegistry

_ALIASES = {
    "tether": "USDT", "usdt": "USDT", "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH", "solana": "SOL", "sol": "SOL",
    "ethereumpow": "ETHW", "usdcoin": "USDC", "usdc": "USDC",
}


@dataclass
class SearchResult:
    coins: list[tuple[str, int]]       # (symbol, active_signal_count)
    exchanges: list[tuple[str, int]]
    rate_limited: bool = False


class SearchService:
    def __init__(self, registry: SignalRegistry, exchange_names: dict[str, str]) -> None:
        self._registry = registry
        self._exchange_names = exchange_names   # id -> display
        self._limiter = SlidingWindowLimiter(max_events=20, window_sec=60)

    @staticmethod
    def _normalize(query: str) -> str:
        cleaned = "".join(c for c in query.lower() if c.isalnum())
        return _ALIASES.get(cleaned, cleaned.upper())

    def search(self, user_id: int, query: str) -> SearchResult:
        if not self._limiter.allow(user_id):
            return SearchResult([], [], rate_limited=True)
        needle = self._normalize(query)
        if len(needle) < 2:
            return SearchResult([], [])

        coin_counts: dict[str, int] = {}
        for signal in self._registry.all_active():
            if needle in signal.coin.upper():
                coin_counts[signal.coin] = coin_counts.get(signal.coin, 0) + 1
        coins = sorted(coin_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]

        exchange_counts: dict[str, int] = {}
        for vid, display in self._exchange_names.items():
            if needle in display.upper() or needle in vid.upper():
                count = sum(
                    1 for s in self._registry.all_active()
                    if vid in (s.buy_exchange, s.sell_exchange)
                )
                exchange_counts[display] = count
        exchanges = sorted(exchange_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return SearchResult(coins=coins, exchanges=exchanges)
