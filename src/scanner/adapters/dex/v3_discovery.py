"""On-chain V3 pool discovery — factory.getPool() over the verified token allowlist.

For every verified base × quote token combination and every supported fee tier we ask
the V3 factory whether a pool exists (one eth_call each); each existing (base, quote,
fee) is a distinct pool. Official RPC only (ARCH-2). Adding coverage is token data, not
code — same model as V2 discovery.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.scanner.adapters.dex.pool_registry import BASE_TOKENS, QUOTE_TOKENS, PoolDef, TokenDef

_GET_POOL_SELECTOR = "0x1698ee82"  # keccak("getPool(address,address,uint24)")[:4]
_ZERO = "0x" + "0" * 64


def _addr_word(address: str) -> str:
    return address[2:].lower().rjust(64, "0")


def _fee_word(fee: int) -> str:
    return f"{fee:064x}"


async def discover_v3_pools(adapter, network: str, factory: str,
                            fee_tiers: tuple[int, ...]) -> list[PoolDef]:
    """Query `factory` for every verified base×quote×feeTier pool on `network`.
    Returns [] on total RPC failure so callers can keep their previous set."""
    bases: list[TokenDef] = BASE_TOKENS.get(network, [])
    quotes: list[TokenDef] = QUOTE_TOKENS.get(network, [])
    # Fan the getPool() probes out concurrently. They were issued one-at-a-time, so a
    # network with N bases × M quotes × K fee tiers took N·M·K serial RPC round-trips
    # (e.g. 60 on Ethereum) — long enough to blow past the DEX staleness window and flap
    # the venue to Maintenance. The adapter's token-bucket still caps the real request
    # rate; gather only overlaps the round-trip latency. Order is irrelevant here.
    combos = [(b, q, fee) for b in bases for q in quotes for fee in fee_tiers]
    results = await asyncio.gather(
        *(adapter.eth_call(factory, _GET_POOL_SELECTOR + _addr_word(b.address)
                           + _addr_word(q.address) + _fee_word(fee))
          for (b, q, fee) in combos),
        return_exceptions=True,
    )
    pools: list[PoolDef] = []
    failures = 0
    for (base, quote, fee), result in zip(combos, results, strict=True):
        if isinstance(result, Exception) or result is None:
            failures += 1
            continue
        if result == _ZERO or int(result, 16) == 0:
            continue  # no pool at this fee tier
        pool_address = "0x" + result[-40:]
        pools.append(PoolDef(
            base_asset=base.symbol, quote_asset=quote.symbol,
            pool_address=pool_address,
            token0_is_base=base.address.lower() < quote.address.lower(),
            base_decimals=base.decimals, quote_decimals=quote.decimals,
            fee_tier=Decimal(fee) / Decimal(1_000_000),  # uint24 → fraction
        ))
    if failures and not pools:
        return []
    return pools
