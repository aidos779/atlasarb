"""Uniswap adapter — V2 pools via factory discovery + RPC reserves reads (ARCH-2).

One adapter instance per network. The pool universe is discovered on-chain from the
Uniswap V2 factory over the verified token allowlist; the curated registry entry is
only the cold-start fallback. Adding coverage is data (pool_registry tokens), not code.
"""
from __future__ import annotations

from src.scanner.adapters.dex.pool_registry import (
    UNISWAP_FEE,
    UNISWAP_POOLS,
    UNISWAP_V2_FACTORY,
)
from src.scanner.adapters.dex.v2_adapter import V2DexAdapter


class UniswapAdapter(V2DexAdapter):
    display_name = "Uniswap"

    def __init__(self, settings, config, sink, network: str, **kw) -> None:
        super().__init__(
            settings, config, sink, network, venue_id=f"uniswap_{network}",
            factory=UNISWAP_V2_FACTORY.get(network), fee_tier=UNISWAP_FEE,
            fallback_pools=UNISWAP_POOLS.get(network, []), **kw)
