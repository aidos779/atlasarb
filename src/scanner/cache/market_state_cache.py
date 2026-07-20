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
from src.domain.market import (
    CanonicalSymbol,
    FundingRate,
    OrderBook,
    PriceQuote,
    is_supported_quote,
)
from src.scanner import mathx

log = get_logger("scanner.cache")

CacheEvent = Callable[[str, str, str], None]  # (base_asset, quote_asset, venue)


class MarketStateCache:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config
        self._prices: dict[tuple[str, str], PriceQuote] = {}      # (venue, pair) -> quote
        self._books: dict[tuple[str, str], OrderBook] = {}
        self._funding: dict[tuple[str, str], FundingRate] = {}    # (venue, base) -> funding
        # Rolling history of recent funding rates per (venue, base) — feeds the funding
        # confidence model's stability/volatility factor (§11.5).
        self._funding_windows: dict[tuple[str, str], deque[Decimal]] = defaultdict(
            lambda: deque(maxlen=self._config.funding_history_window)
        )
        self._price_windows: dict[tuple[str, str], deque[Decimal]] = defaultdict(
            lambda: deque(maxlen=self._config.outlier_window_ticks)
        )
        self._warmup: dict[tuple[str, str], int] = defaultdict(int)
        self._tracked: set[tuple[str, str]] = set()               # (venue, pair)
        # pair -> venues holding a price or book entry for it. Kept in lockstep with
        # _prices/_books (written in upsert_price/upsert_book, removed only in untrack —
        # the sole removal point for either dict) so venues_for_pair() and the cross-venue
        # outlier check are O(venues) instead of scanning the whole cache. That scan made
        # detection quadratic in universe size (~40 s full scans at ~8k pairs) and ran on
        # every WS price write via _cross_venue_suspect.
        self._venues_by_pair: dict[str, set[str]] = defaultdict(set)
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
        # Non-USDT quotes (§3.6) never earn a cache slot. upsert_price/upsert_book both
        # gate on _tracked, so refusing here is what keeps their books and snapshots out
        # of memory entirely rather than merely unread.
        if not is_supported_quote(symbol.quote_asset):
            log.debug("track_rejected_quote", venue=venue, pair=symbol.pair)
            return
        self._tracked.add((venue, symbol.pair))

    def untrack(self, venue: str, pair: str) -> None:
        key = (venue, pair)
        self._tracked.discard(key)
        self._prices.pop(key, None)
        self._books.pop(key, None)
        self._price_windows.pop(key, None)
        self._warmup.pop(key, None)
        # Both backing entries are gone — drop the venue from the pair index, and the
        # pair's set entirely once empty so the index never outgrows the live cache.
        venues = self._venues_by_pair.get(pair)
        if venues is not None:
            venues.discard(venue)
            if not venues:
                del self._venues_by_pair[pair]

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
        self._venues_by_pair[quote.symbol.pair].add(quote.venue)
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
        self._venues_by_pair[book.symbol.pair].add(book.venue)
        # DEX venues publish pool state as books only (no PriceQuote stream), so the
        # §3.1 warm-up counter must advance on pool reads too — otherwise DEX pairs
        # never warm up and every CEX-DEX / DEX-DEX candidate dies NOT_WARMED_UP.
        if book.pool_address is not None and self._warmup[key] < self._config.warmup_samples:
            self._warmup[key] += 1
        self._emit(book.symbol.base_asset, book.symbol.quote_asset, book.venue)

    def upsert_funding(self, funding: FundingRate) -> None:
        key = (funding.venue, funding.base_asset)
        self._funding[key] = funding
        self._funding_windows[key].append(funding.current_rate)

    def funding_window(self, venue: str, base_asset: str) -> list[Decimal]:
        """Recent funding-rate history for one (venue, base) — for the confidence
        stability/volatility factor."""
        return list(self._funding_windows.get((venue, base_asset), ()))

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
        # Via the pair index (O(venues)), not a full _prices scan — this runs on every
        # WS price write. Book-only venues have no _prices entry and drop out, exactly
        # as they did under the old scan.
        others = []
        for v in self._venues_by_pair.get(pair, ()):
            if v == quote.venue:
                continue
            q = self._prices.get((v, pair))
            if q is not None and q.mid > 0:
                others.append(q.mid)
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
        # DEX-DEX or cross-chain candidates could ever form). Served from the
        # maintained pair index — the previous full scan of _prices+_books made this
        # O(cache size) per call, ~6 calls per symbol, i.e. quadratic per full scan.
        return list(self._venues_by_pair.get(pair, ()))

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
