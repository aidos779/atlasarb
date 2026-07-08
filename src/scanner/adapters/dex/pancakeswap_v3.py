"""PancakeSwap V3 adapter — concentrated-liquidity pools via factory discovery + RPC.

Distinct venue from PancakeSwap V2; the two form real same-chain DEX↔DEX pairs on BNB
Chain. Pools read as virtual reserves (v3_reader) through the shared DEX leg/profit path.
"""
from __future__ import annotations

from src.scanner.adapters.dex.pool_registry import PANCAKE_V3_FACTORY, PANCAKE_V3_FEE_TIERS
from src.scanner.adapters.dex.v2_adapter import V3DexAdapter


class PancakeSwapV3Adapter(V3DexAdapter):
    display_name = "PancakeSwap V3"

    def __init__(self, settings, config, sink, network: str = "bnb", **kw) -> None:
        super().__init__(
            settings, config, sink, network, venue_id=f"pancakeswap_v3_{network}",
            factory=PANCAKE_V3_FACTORY.get(network), fee_tiers=PANCAKE_V3_FEE_TIERS, **kw)
