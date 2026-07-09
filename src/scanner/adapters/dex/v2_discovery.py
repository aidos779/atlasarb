"""On-chain V2 pool discovery — factory.getPair() over the verified token allowlist.

Replaces the hand-curated pool list as the primary coverage source: for every verified
base × quote token combination on a network we ask the DEX factory whether a pool
exists (one eth_call each) and synthesize a PoolDef for those that do. Zero third-party
indexers involved (ARCH-2 — official RPC only), and coverage grows by editing token
data, not code.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.config import get_logger
from src.scanner.adapters.dex.pool_registry import BASE_TOKENS, QUOTE_TOKENS, PoolDef, TokenDef

log = get_logger("adapter.dex")

_GET_PAIR_SELECTOR = "0xe6a43905"  # keccak("getPair(address,address)")[:4]
_ZERO = "0x" + "0" * 64


def _addr_word(address: str) -> str:
    return address[2:].lower().rjust(64, "0")


async def discover_v2_pools(adapter, network: str, factory: str,
                            fee_tier: Decimal) -> list[PoolDef]:
    """Query `factory` for every verified base×quote pool on `network`.
    Returns [] on total RPC failure so callers can keep their previous set."""
    bases: list[TokenDef] = BASE_TOKENS.get(network, [])
    quotes: list[TokenDef] = QUOTE_TOKENS.get(network, [])
    # Concurrent getPair() probes (see v3_discovery for the rationale): serial round-trips
    # over a rate-limited public RPC were a source of DEX staleness/Maintenance flaps. The
    # token bucket still bounds the real request rate.
    combos = [(b, q) for b in bases for q in quotes]
    results = await asyncio.gather(
        *(adapter.eth_call(factory, _GET_PAIR_SELECTOR + _addr_word(b.address)
                           + _addr_word(q.address))
          for (b, q) in combos),
        return_exceptions=True,
    )
    pools: list[PoolDef] = []
    failures = 0
    for (base, quote), result in zip(combos, results, strict=True):
        if isinstance(result, Exception) or result is None:
            failures += 1
            continue
        if result == _ZERO or int(result, 16) == 0:
            continue  # factory has no pool for this combination
        pool_address = "0x" + result[-40:]
        pools.append(PoolDef(
            base_asset=base.symbol, quote_asset=quote.symbol,
            pool_address=pool_address,
            token0_is_base=base.address.lower() < quote.address.lower(),
            base_decimals=base.decimals, quote_decimals=quote.decimals,
            fee_tier=fee_tier,
        ))
    if failures and not pools:
        return []
    return pools
