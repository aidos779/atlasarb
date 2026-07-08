"""Shared pool-based DEX adapters (V2 constant-product and V3 concentrated liquidity).

`_PoolDexAdapter` owns the universe for one (venue, network): it starts from a curated
cold-start fallback, swaps in the factory-discovered set on the DEX discovery interval,
and reads each pool through the family-specific reader. `V2DexAdapter` and `V3DexAdapter`
supply only their discovery + read functions — everything downstream (leg model, profit
engine) is shared, because both families expose the same (reserve_base, reserve_quote)
view (V3 via virtual reserves; see v3_reader).
"""
from __future__ import annotations

import time
from abc import abstractmethod
from decimal import Decimal

from src.config import get_logger
from src.domain.enums import VenueType
from src.domain.market import CanonicalSymbol, OrderBook
from src.scanner.adapters.base_dex import BaseDexAdapter
from src.scanner.adapters.dex.pool_registry import PoolDef
from src.scanner.adapters.dex.v2_discovery import discover_v2_pools
from src.scanner.adapters.dex.v2_reader import read_v2_pool
from src.scanner.adapters.dex.v3_discovery import discover_v3_pools
from src.scanner.adapters.dex.v3_reader import read_v3_pool

log = get_logger("adapter.dex")


class _PoolDexAdapter(BaseDexAdapter):
    def __init__(self, settings, config, sink, network: str, venue_id: str,
                 factory: str | None, fallback_pools: list[PoolDef], **kw) -> None:
        super().__init__(settings, config, sink, network, rate_per_sec=10, burst=20, **kw)
        self.id = venue_id
        self._factory = factory
        self._pools: list[PoolDef] = list(fallback_pools)
        self._by_pair: dict[str, PoolDef] = self._index(self._pools)
        self._discovered_at: float = 0.0

    @staticmethod
    def _index(pools: list[PoolDef]) -> dict[str, PoolDef]:
        # setdefault → first-wins. Discovery yields fee tiers ascending, so the
        # lowest-fee (typically deepest for majors/stables) pool represents each pair.
        by_pair: dict[str, PoolDef] = {}
        for p in pools:
            by_pair.setdefault(f"{p.base_asset}/{p.quote_asset}", p)
        return by_pair

    @abstractmethod
    async def _discover(self) -> list[PoolDef]:
        """Factory discovery for this pool family. [] on RPC failure (keep prior set)."""

    @abstractmethod
    async def _read(self, pool: PoolDef) -> OrderBook | None: ...

    async def _maybe_discover(self) -> None:
        if self._factory is None:
            return
        now = time.time()
        if (self._discovered_at
                and now - self._discovered_at < self._config.dex_discovery_interval_sec):
            return
        pools = await self._discover()
        if not pools:
            return  # RPC trouble — keep the current (fallback/previous) set
        self._discovered_at = now
        by_pair = self._index(pools)
        if len(by_pair) != len(self._by_pair):
            log.info("dex_pools_discovered", venue=self.id, network=self.network,
                     pools=len(by_pair))
        self._pools = pools
        self._by_pair = by_pair

    async def _list_pools(self) -> list[CanonicalSymbol]:
        await self._maybe_discover()
        return [CanonicalSymbol(p.base_asset, p.quote_asset, VenueType.DEX, self.network)
                for p in self._by_pair.values()]

    async def _read_pool(self, symbol: CanonicalSymbol) -> OrderBook | None:
        pool = self._by_pair.get(symbol.pair)
        if pool is None:
            return None
        return await self._read(pool)


class V2DexAdapter(_PoolDexAdapter):
    def __init__(self, settings, config, sink, network: str, venue_id: str,
                 factory: str | None, fee_tier: Decimal,
                 fallback_pools: list[PoolDef], **kw) -> None:
        super().__init__(settings, config, sink, network, venue_id, factory,
                         fallback_pools, **kw)
        self._fee_tier = fee_tier
        self._taker_fee = fee_tier  # actual pool fee, not the generic 0.3% default

    async def _discover(self) -> list[PoolDef]:
        return await discover_v2_pools(self, self.network, self._factory, self._fee_tier)

    async def _read(self, pool: PoolDef) -> OrderBook | None:
        return await read_v2_pool(self, self.id, self.network, pool)


class V3DexAdapter(_PoolDexAdapter):
    def __init__(self, settings, config, sink, network: str, venue_id: str,
                 factory: str | None, fee_tiers: tuple[int, ...],
                 fallback_pools: list[PoolDef] | None = None, **kw) -> None:
        super().__init__(settings, config, sink, network, venue_id, factory,
                         fallback_pools or [], **kw)
        self._fee_tiers = fee_tiers
        # V3 fee is per-pool (varies by tier); taker_fee comes off each pool's tier
        # at read time via book.pool_fee_tier, so no single adapter-wide default.

    async def _discover(self) -> list[PoolDef]:
        return await discover_v3_pools(self, self.network, self._factory, self._fee_tiers)

    async def _read(self, pool: PoolDef) -> OrderBook | None:
        return await read_v3_pool(self, self.id, self.network, pool)
