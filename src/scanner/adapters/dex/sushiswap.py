"""SushiSwap adapter — V2 pools via factory discovery + RPC reserves reads (ARCH-2).

A second Ethereum DEX venue so same-chain DEX↔DEX arbitrage (Uniswap vs SushiSwap)
has two live legs; identical V2 mechanics, own factory."""
from __future__ import annotations

from src.scanner.adapters.dex.pool_registry import (
    SUSHI_FEE,
    SUSHI_POOLS,
    SUSHISWAP_V2_FACTORY,
)
from src.scanner.adapters.dex.v2_adapter import V2DexAdapter


class SushiSwapAdapter(V2DexAdapter):
    display_name = "SushiSwap"

    def __init__(self, settings, config, sink, network: str = "ethereum", **kw) -> None:
        super().__init__(
            settings, config, sink, network, venue_id=f"sushiswap_{network}",
            factory=SUSHISWAP_V2_FACTORY.get(network), fee_tier=SUSHI_FEE,
            fallback_pools=SUSHI_POOLS.get(network, []), **kw)
