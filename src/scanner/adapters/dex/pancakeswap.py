"""PancakeSwap adapter — V2 pools via factory discovery + BNB Chain RPC (ARCH-2)."""
from __future__ import annotations

from src.scanner.adapters.dex.pool_registry import (
    PANCAKE_FEE,
    PANCAKE_POOLS,
    PANCAKE_V2_FACTORY,
)
from src.scanner.adapters.dex.v2_adapter import V2DexAdapter


class PancakeSwapAdapter(V2DexAdapter):
    display_name = "PancakeSwap"

    def __init__(self, settings, config, sink, network: str = "bnb", **kw) -> None:
        super().__init__(
            settings, config, sink, network, venue_id=f"pancakeswap_{network}",
            factory=PANCAKE_V2_FACTORY.get(network), fee_tier=PANCAKE_FEE,
            fallback_pools=PANCAKE_POOLS.get(network, []), **kw)
