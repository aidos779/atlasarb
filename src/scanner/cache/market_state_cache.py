"""Market State Cache — in-memory canonical market state (Scanner §1.1 principle 3).

Responsibilities:
  - Store latest price/book/funding per (venue, canonical symbol).
  - Invalid-price rejection (§4.5) and robust outlier detection (§4.6) before write.
  - Warm-up tracking (§3.1): a symbol is signal-eligible only after N good samples.
  - Emit a cache-write event with the affected (base,quote) set (§1.4 event trigger).
  - Reject writes for untracked pairs (§3.2 removal precedence).
  - Freshness introspection so detectors skip stale legs (§4.4).

Process-local for MVP (§1.5); a Redis-backed sink is a documented future extension.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from decimal import Decimal

from src.config import get_logger
from src.config.scanner_config import ScannerConfig
from src.domain.market import CanonicalSymbol, FundingRate, OrderBook, PriceQuote
from src.scanner import mathx

log = get_logger("scanner.cache")

CacheEvent = Callable[[str, str, str], None]  # (base_asset, quote_asset, venue)


class MarketStateCache:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config
        self._prices: dict[tuple[str, str], PriceQuote] = {}      # (venue, pair) -> quote
        self._books: dict[tuple[str, str], OrderBook] = {}
        self._funding: dict[tuple[str, str], FundingRate] = {}    # (venue, base) -> funding
        self._price_windows: dict[tuple[str, str], deque[Decimal]] = defaultdict(
            lambda: deque(maxlen=self._config.outlier_window_ticks)
        )
        self._warmup: dict[tuple[str, str], int] = defaultdict(int)
        self._tracked: set[tuple[str, str]] = set()               # (venue, pair)
        self._listeners: list[CacheEvent] = []
        self._suspect: dict[tuple[str, str], bool] = {}           # cross-venue flag
        self.rejected_prices = 0
        self.outliers = 0

    # ── configuration / lifecycle ──
    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def subscribe(self, listener: CacheEvent) -> None:
        self._listeners.append(listener)

    def track(self, venue: str, symbol: CanonicalSymbol) -> None:
        self._tracked.add((venue, symbol.pair))

    def untrack(self, venue: str, pair: str) -> None:
        key = (venue, pair)
        self._tracked.discard(key)
        self._prices.pop(key, None)
        self._books.pop(key, None)
        self._price_windows.pop(key, None)
        self._warmup.pop(key, None)

    def is_tracked(self, venue: str, pair: str) -> bool:
        return (venue, pair) in self._tracked

    # ── writes ──
    def upsert_price(self, quote: PriceQuote) -> None:
        key = (quote.venue, quote.symbol.pair)
        if key not in self._tracked:
            # §3.2 — cache writes for untracked pairs are rejected.
            return
        if not self._valid_price(quote, key):
            self.rejected_prices += 1
            return
        window = self._price_windows[key]
        if mathx.is_mad_outlier(quote.mid, list(window), Decimal(str(self._config.outlier_mad_k))):
            self.outliers += 1
            log.debug("outlier_rejected", venue=quote.venue, pair=quote.symbol.pair,
                      mid=str(quote.mid))
            return
        if self._cross_venue_suspect(quote):
            # §4.6 secondary layer: require confirmation on next tick before seeding.
            prev = self._suspect.get(key, False)
            self._suspect[key] = True
            if not prev:
                return  # first suspect reading not yet confirmed
        else:
            self._suspect[key] = False
        window.append(quote.mid)
        self._prices[key] = quote
        if self._warmup[key] < self._config.warmup_samples:
            self._warmup[key] += 1
        self._emit(quote.symbol.base_asset, quote.symbol.quote_asset, quote.venue)

    def upsert_book(self, book: OrderBook) -> None:
        key = (book.venue, book.symbol.pair)
        if key not in self._tracked:
            return
        # §5.6 — corrupted books never reach the cache.
        if book.is_crossed() or book.is_empty_side():
            log.debug("book_rejected", venue=book.venue, pair=book.symbol.pair)
            return
        self._books[key] = book
        # DEX venues publish pool state as books only (no PriceQuote stream), so the
        # §3.1 warm-up counter must advance on pool reads too — otherwise DEX pairs
        # never warm up and every CEX-DEX / DEX-DEX candidate dies NOT_WARMED_UP.
        if book.pool_address is not None and self._warmup[key] < self._config.warmup_samples:
            self._warmup[key] += 1
        self._emit(book.symbol.base_asset, book.symbol.quote_asset, book.venue)

    def upsert_funding(self, funding: FundingRate) -> None:
        self._funding[(funding.venue, funding.base_asset)] = funding

    # ── validation (§4.5) ──
    def _valid_price(self, quote: PriceQuote, key: tuple[str, str]) -> bool:
        if quote.bid <= 0 or quote.ask <= 0 or quote.last <= 0:
            return False
        if quote.bid > quote.ask:  # crossed book (tolerance 0 for CEX top-of-book)
            return False
        now = time.time()
        if quote.ts > now + 2.0:  # future timestamp beyond clock skew
            return False
        prev = self._prices.get(key)
        if prev is not None:
            if quote.ts < prev.ts:  # older than message it replaces
                return False
            dev = mathx.pct_deviation(quote.mid, prev.mid)
            if dev > Decimal(str(self._config.max_sane_spread_pct)):
                return False  # fat-finger / bad tick
        return True

    def _cross_venue_suspect(self, quote: PriceQuote) -> bool:
        """§4.6 — flag a single-venue reading deviating >X% from cross-venue median."""
        pair = quote.symbol.pair
        others = [
            q.mid for (v, p), q in self._prices.items()
            if p == pair and v != quote.venue and q.mid > 0
        ]
        if len(others) < 2:
            return False
        med = mathx.robust_median(others)
        return mathx.pct_deviation(quote.mid, med) > Decimal(
            str(self._config.cross_venue_deviation_pct)
        )

    # ── reads (freshness-aware, §4.4) ──
    def get_price(self, venue: str, pair: str) -> PriceQuote | None:
        quote = self._prices.get((venue, pair))
        if quote is None:
            return None
        max_age = self._config.max_age_cex_price_sec  # DEX overridden by adapter source
        if quote.source in ("RPC", "QUOTE"):
            max_age = self._config.max_age_dex_price_sec
        if quote.staleness() > max_age:
            return None  # treated as unavailable, never last-known-good (§4.4)
        return quote

    def get_book(self, venue: str, pair: str) -> OrderBook | None:
        book = self._books.get((venue, pair))
        if book is None:
            return None
        is_dex = book.pool_address is not None
        max_age = 1e9 if is_dex else self._config.max_age_orderbook_cex_sec
        if not is_dex and book.staleness() > max_age:
            return None
        return book

    def get_funding(self, venue: str, base_asset: str) -> FundingRate | None:
        f = self._funding.get((venue, base_asset))
        if f is None or f.staleness() > self._config.max_age_funding_sec:
            return None
        return f

    def venues_for_pair(self, pair: str) -> list[str]:
        # Books too, not just quotes: DEX venues carry pool state as books only —
        # a price-only view hid every DEX venue from the detectors (no CEX-DEX,
        # DEX-DEX or cross-chain candidates could ever form).
        venues = {v for (v, p) in self._prices if p == pair}
        venues |= {v for (v, p) in self._books if p == pair}
        return list(venues)

    def tracked_pairs(self) -> set[str]:
        return {p for (_, p) in self._tracked}

    def all_funding_for(self, base_asset: str) -> list[FundingRate]:
        return [f for (_, b), f in self._funding.items() if b == base_asset]

    def is_warmed_up(self, venue: str, pair: str) -> bool:
        """§3.1 — eligible only after N consecutive good samples."""
        return self._warmup.get((venue, pair), 0) >= self._config.warmup_samples

    def price_window(self, venue: str, pair: str) -> list[Decimal]:
        return list(self._price_windows.get((venue, pair), ()))

    def size(self) -> int:
        return len(self._prices) + len(self._books) + len(self._funding)

    def book_coverage(self) -> tuple[int, int]:
        """(total fresh books, pairs with >=2 venues having a fresh book).

        The second number is the count of pairs that can actually form a spot
        candidate — diagnostic for 'data present but no candidates'."""
        from collections import defaultdict
        per_pair: dict[str, int] = defaultdict(int)
        total = 0
        for (venue, pair) in list(self._books.keys()):
            if self.get_book(venue, pair) is not None:
                total += 1
                per_pair[pair] += 1
        multi = sum(1 for c in per_pair.values() if c >= 2)
        return total, multi

    def _emit(self, base: str, quote: str, venue: str) -> None:
        for listener in self._listeners:
            listener(base, quote, venue)
