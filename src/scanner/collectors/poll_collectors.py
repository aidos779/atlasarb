"""Poll-based collectors for data types without a WS push equivalent (Scanner §4.1/§6).

DEX pool reserves are polled per block (§4.2); funding rates every 60s (§6.1). CEX
price/book arrive via adapter WS streams straight into the cache; these collectors cover
the REST/RPC-fallback data types only, per the WebSocket-first principle (§1.1).
"""
from __future__ import annotations

import asyncio

from src.config import describe_exc, get_logger
from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.domain.ports import ExchangeAdapter
from src.scanner.cache.market_state_cache import MarketStateCache

log = get_logger("scanner.poll")


class DexPoolCollector:
    """Polls DEX pool state per network block interval (§4.2/§5.5).

    Discovery (listing the venue's pools) and quoting (reading their reserves) are kept
    independent: if a discovery call fails, the collector keeps quoting the last known
    pool set, so a stale/failed discovery snapshot never starves the quote feed (and thus
    never drives the venue to Maintenance — §2.2 health = execution capability).
    """

    def __init__(self, config: ScannerConfig, cache: MarketStateCache,
                 adapters: dict[str, ExchangeAdapter], health=None) -> None:
        self._config = config
        self._cache = cache
        self._health = health
        self._adapters = {v: a for v, a in adapters.items() if a.venue_type == VenueType.DEX}
        # Last successfully-discovered pool set per venue — reused for quoting when a
        # later discovery call fails, so quoting is decoupled from discovery health.
        self._last_markets: dict[str, list] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    async def poll_once(self) -> None:
        # Poll every DEX venue concurrently. Serially, one slow/rate-limited network
        # (Ethereum public RPC especially) delayed every venue behind it, pushing pool
        # data past the DEX staleness window (stale_dex_rpc_sec) and flapping those
        # venues to Maintenance even though they were reachable.
        await asyncio.gather(
            *(self._poll_venue(v, a) for v, a in self._adapters.items()),
            return_exceptions=True,
        )

    async def _poll_venue(self, venue: str, adapter: ExchangeAdapter) -> None:
        # ── discovery (independent of quoting) ──
        try:
            markets = await adapter.get_markets()
            self._last_markets[venue] = markets
            if self._health is not None:
                self._health.record_discovery(venue)
        except Exception as exc:  # noqa: BLE001
            # Discovery failed — fall back to the last known pool set so quoting
            # continues. Only if we have never discovered anything do we stop here.
            markets = self._last_markets.get(venue)
            if not markets:
                log.debug("dex_market_poll_failed", venue=venue, error=describe_exc(exc))
                return
            log.debug("dex_discovery_stale_quoting_cached", venue=venue,
                      pools=len(markets), error=describe_exc(exc))
        for sym in markets:
            if not self._cache.is_tracked(venue, sym.pair):
                self._cache.track(venue, sym)

        # Read this venue's pools concurrently. The adapter's token bucket bounds the
        # real RPC rate; gather only overlaps round-trip latency so a full cycle stays
        # well inside the staleness window.
        async def _read(sym):
            try:
                return await adapter.get_pool_state(sym)
            except Exception:  # noqa: BLE001
                return None

        books = await asyncio.gather(*(_read(s) for s in markets),
                                     return_exceptions=True)
        for book in books:
            if book is not None and not isinstance(book, Exception):
                self._cache.upsert_book(book)

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="dex-poll")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("dex_poll_error", error=describe_exc(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except TimeoutError:
                pass


class FundingCollector:
    """Polls perpetual funding rate + next funding time every 60s (§6.1)."""

    def __init__(self, config: ScannerConfig, cache: MarketStateCache,
                 adapters: dict[str, ExchangeAdapter], perp_assets: set[str]) -> None:
        self._config = config
        self._cache = cache
        self._adapters = {v: a for v, a in adapters.items() if a.venue_type == VenueType.CEX}
        self._perp_assets = perp_assets
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def poll_once(self) -> None:
        for adapter in self._adapters.values():
            for asset in self._perp_assets:
                try:
                    funding = await adapter.get_funding_rate(asset)
                except Exception:  # noqa: BLE001
                    continue
                if funding is not None:
                    self._cache.upsert_funding(funding)

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="funding-poll")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("funding_poll_error", error=describe_exc(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60.0)
            except TimeoutError:
                pass
