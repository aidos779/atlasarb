"""Shared pool-based DEX adapters (V2 constant-product and V3 concentrated liquidity).

`_PoolDexAdapter` owns the universe for one (venue, network): it starts from a curated
cold-start fallback, swaps in the factory-discovered set on the DEX discovery interval,
and reads each pool through the family-specific reader. `V2DexAdapter` and `V3DexAdapter`
supply only their discovery + read functions — everything downstream (leg model, profit
engine) is shared, because both families expose the same (reserve_base, reserve_quote)
view (V3 via virtual reserves; see v3_reader).
"""
from __future__ import annotations

import asyncio
import time
from abc import abstractmethod
from decimal import Decimal

from src.config import get_logger
from src.domain.enums import ExchangeStatus, VenueType
from src.domain.market import CanonicalSymbol, OrderBook
from src.scanner.adapters.base_dex import BaseDexAdapter
from src.scanner.adapters.dex.multicall import DEFAULT_MAX_CALLS_PER_BATCH, multicall_read
from src.scanner.adapters.dex.pool_registry import PoolDef
from src.scanner.adapters.dex.v2_discovery import discover_v2_pools
from src.scanner.adapters.dex.v2_reader import decode_v2_pool, plan_v2_calls, read_v2_pool
from src.scanner.adapters.dex.v3_discovery import discover_v3_pools
from src.scanner.adapters.dex.v3_reader import decode_v3_pool, plan_v3_calls, read_v3_pool

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

    # ── Multicall3 batching (§2.4 RPC-load reduction) ──
    # Sub-calls each pool contributes to a batch (V2: 1 getReserves; V3: slot0+liquidity).
    _calls_per_pool: int = 1

    @abstractmethod
    def _plan_calls(self, pool: PoolDef) -> list[tuple[str, str]]:
        """(target, calldata) tuples this pool needs, in decode order."""

    @abstractmethod
    def _decode_pool(self, pool: PoolDef, results: list[str | None]) -> OrderBook | None:
        """Reconstruct the pool's OrderBook from its slice of the batch results."""

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

    async def read_pools(self, symbols: list[CanonicalSymbol]) -> list[OrderBook]:
        """Read many pools per cycle with the fewest RPC requests possible.

        Batches every pool's calls into Multicall3 ``eth_call``s (one request per ~60
        sub-calls instead of one per pool) — the direct remedy for the public-node
        rate-limit storms. Falls back to concurrent per-pool reads if the multicall path
        is unavailable on this network or fails to decode, so behaviour degrades to the old
        path rather than dropping data. Mirrors get_pool_state's freshness accounting: each
        successful book counts as a live stream tick (§4.2) and marks the venue Online.
        """
        pools = [p for s in symbols if (p := self._by_pair.get(s.pair)) is not None]
        if not pools:
            return []
        books = await self._read_pools_multicall(pools)
        if books is None:  # multicall unusable → concurrent per-pool fallback
            results = await asyncio.gather(*(self._read(p) for p in pools),
                                           return_exceptions=True)
            books = [b for b in results if isinstance(b, OrderBook)]
        for _ in books:
            self._on_success(stream=True)
        if books:
            self._status = ExchangeStatus.ONLINE
        return books

    async def _read_pools_multicall(self, pools: list[PoolDef]) -> list[OrderBook] | None:
        """Read all pools via Multicall3, chunked to bound request size. Returns None (so
        the caller falls back) if any chunk's multicall fails to execute/decode."""
        max_pools = max(1, DEFAULT_MAX_CALLS_PER_BATCH // max(1, self._calls_per_pool))
        books: list[OrderBook] = []
        for i in range(0, len(pools), max_pools):
            chunk = pools[i:i + max_pools]
            calls: list[tuple[str, str]] = []
            spans: list[tuple[PoolDef, int, int]] = []
            for p in chunk:
                pcalls = self._plan_calls(p)
                spans.append((p, len(calls), len(pcalls)))
                calls.extend(pcalls)
            results = await multicall_read(self, calls)
            if results is None:
                return None
            for p, start, count in spans:
                book = self._decode_pool(p, results[start:start + count])
                if book is not None:
                    books.append(book)
        return books


class V2DexAdapter(_PoolDexAdapter):
    def __init__(self, settings, config, sink, network: str, venue_id: str,
                 factory: str | None, fee_tier: Decimal,
                 fallback_pools: list[PoolDef], **kw) -> None:
        super().__init__(settings, config, sink, network, venue_id, factory,
                         fallback_pools, **kw)
        self._fee_tier = fee_tier
        # V2 has one fee tier per factory, so taker_fee() can carry the real rate.
        # Not used by the spot profit pipeline either way (see _charges_flat_taker_fee):
        # the pool fee reaches the maths through book.pool_fee_tier / DexPoolLeg.
        self._taker_fee = fee_tier

    _calls_per_pool = 1

    async def _discover(self) -> list[PoolDef]:
        return await discover_v2_pools(self, self.network, self._factory, self._fee_tier)

    async def _read(self, pool: PoolDef) -> OrderBook | None:
        return await read_v2_pool(self, self.id, self.network, pool)

    def _plan_calls(self, pool: PoolDef) -> list[tuple[str, str]]:
        return plan_v2_calls(pool)

    def _decode_pool(self, pool: PoolDef, results: list[str | None]) -> OrderBook | None:
        return decode_v2_pool(self.id, self.network, pool, results)


class V3DexAdapter(_PoolDexAdapter):
    def __init__(self, settings, config, sink, network: str, venue_id: str,
                 factory: str | None, fee_tiers: tuple[int, ...],
                 fallback_pools: list[PoolDef] | None = None, **kw) -> None:
        super().__init__(settings, config, sink, network, venue_id, factory,
                         fallback_pools or [], **kw)
        self._fee_tiers = fee_tiers
        # V3 fee is per-pool (0.01% / 0.05% / 0.3% / 1%), so there is no adapter-wide
        # value to set here and taker_fee() stays at the base flat 0.3%. That is
        # deliberate and harmless: the spot profit pipeline does not read taker_fee()
        # for DEX legs at all. Each pool's real tier reaches the maths as
        # book.pool_fee_tier -> DexPoolLeg.fee_rate -> mathx.amm_output, i.e. through
        # the fill price. See assembler._charges_flat_taker_fee.

    _calls_per_pool = 2

    async def _discover(self) -> list[PoolDef]:
        return await discover_v3_pools(self, self.network, self._factory, self._fee_tiers)

    async def _read(self, pool: PoolDef) -> OrderBook | None:
        return await read_v3_pool(self, self.id, self.network, pool)

    def _plan_calls(self, pool: PoolDef) -> list[tuple[str, str]]:
        return plan_v3_calls(pool)

    def _decode_pool(self, pool: PoolDef, results: list[str | None]) -> OrderBook | None:
        return decode_v3_pool(self.id, self.network, pool, results)
