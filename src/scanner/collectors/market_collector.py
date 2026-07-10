"""Market Collector (Scanner §3) — discovery, delisting, warm-up gating.

Maintains the canonical tradable-pair set per venue. Restricts to USDT/USDC spot
(R-QUOTE-1), auto-discovers new pairs (BR-ASSET-1/2), and removes delisted pairs after
a grace period (§3.2), force-expiring any open signals referencing them.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from src.config import describe_exc, get_logger
from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.domain.market import CanonicalSymbol
from src.domain.ports import ExchangeAdapter
from src.scanner.adapters.backoff import backoff_delay
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.priority.scheduler import PriorityClassifier

log = get_logger("scanner.discovery")

ForceExpireDelisted = Callable[[str], Awaitable[int]]


class MarketCollector:
    def __init__(
        self, config: ScannerConfig, cache: MarketStateCache,
        adapters: dict[str, ExchangeAdapter], priority: PriorityClassifier,
        force_expire_delisted: ForceExpireDelisted, health=None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._adapters = adapters
        self._priority = priority
        self._force_expire = force_expire_delisted
        self._health = health
        self._tracked: dict[str, set[str]] = defaultdict(set)     # venue -> pairs
        self._missing_since: dict[tuple[str, str], float] = {}
        self._discovery_failed: set[str] = set()   # venues currently failing discovery
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def tracked_pairs(self) -> set[str]:
        pairs: set[str] = set()
        for venue_pairs in self._tracked.values():
            pairs |= venue_pairs
        return pairs

    async def _get_markets_retry(self, adapter: ExchangeAdapter) -> list[CanonicalSymbol]:
        """Bounded retry so a transient timeout doesn't blank a venue until the next
        discovery cycle (minutes away). Exponential backoff with jitter between
        attempts (each retry may land on a rotated host/proxy — see BaseCexAdapter).
        Re-raises the last error if all attempts fail."""
        last: Exception | None = None
        for attempt in range(self._config.rest_max_attempts):
            try:
                return await adapter.get_markets()
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt + 1 < self._config.rest_max_attempts:
                    await asyncio.sleep(backoff_delay(
                        attempt, base=0.5, multiplier=2.0, cap=10.0, jitter=0.5))
        assert last is not None
        raise last

    async def discover_once(self) -> None:
        for venue, adapter in self._adapters.items():
            try:
                markets = await self._get_markets_retry(adapter)
            except Exception as exc:  # noqa: BLE001
                # Warn once per outage; stay at debug while it remains down so a
                # persistently unreachable venue doesn't flood the logs each cycle.
                if venue in self._discovery_failed:
                    log.debug("discovery_failed", venue=venue, error=describe_exc(exc))
                else:
                    log.warning("discovery_failed", venue=venue, error=describe_exc(exc))
                    self._discovery_failed.add(venue)
                continue
            if venue in self._discovery_failed:
                self._discovery_failed.discard(venue)
                log.info("discovery_recovered", venue=venue)
            # Discovery is an independent signal from quote freshness (§2.2): record it
            # for monitoring, but it never drives maintenance/offline on its own.
            if self._health is not None:
                self._health.record_discovery(venue)
            current = {m.pair for m in markets if m.quote_asset in ("USDT", "USDC")}
            await self._apply_discovery(venue, adapter, markets, current)
        # Refresh priority top-100 from breadth of tracked pairs (proxy).
        self._priority.set_top100({p.split("/")[0] for p in self.tracked_pairs()})

    async def _apply_discovery(
        self, venue: str, adapter: ExchangeAdapter,
        markets: list[CanonicalSymbol], current: set[str],
    ) -> None:
        now = time.time()
        known = self._tracked[venue]
        new_symbols = [m for m in markets if m.pair in current and m.pair not in known]
        for sym in new_symbols:
            known.add(sym.pair)
            self._cache.track(venue, sym)
            # Per-pair discovery is DEBUG only — thousands of these flooded the logs
            # on first scan. The per-venue INFO summary below preserves diagnostics.
            log.debug("pair_discovered", venue=venue, pair=sym.pair)
        # Subscribe streams for new CEX symbols; DEX pulled via pool polling.
        if new_symbols and adapter.venue_type == VenueType.CEX:
            await adapter.subscribe_ticker(new_symbols)
            await adapter.subscribe_order_book(new_symbols, depth=20)
        if new_symbols:
            log.info("discovery_updated", venue=venue,
                     new=len(new_symbols), tracked=len(known))

        # Delisting handling (§3.2).
        for pair in list(known):
            key = (venue, pair)
            if pair in current:
                self._missing_since.pop(key, None)
                continue
            first_missing = self._missing_since.setdefault(key, now)
            if now - first_missing >= self._config.delist_grace_period_sec:
                known.discard(pair)
                self._cache.untrack(venue, pair)
                self._missing_since.pop(key, None)
                await self._force_expire(pair)
                log.info("pair_delisted", venue=venue, pair=pair)

    def start(self) -> None:
        self._stop.clear()
        self._tasks = [asyncio.create_task(self._loop(), name="discovery")]

    async def stop(self) -> None:
        self._stop.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self) -> None:
        await self.discover_once()
        while not self._stop.is_set():
            interval = min(self._config.cex_discovery_interval_sec,
                           self._config.dex_discovery_interval_sec)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                await self.discover_once()
