"""Uniswap V3 adapter — concentrated-liquidity pools via factory discovery + RPC.

Pools discovered from the Uniswap V3 factory (getPool over verified tokens × fee tiers);
each pool read as virtual reserves (v3_reader) so it reuses the shared DEX leg/profit
path. A distinct venue from Uniswap V2 — the two form real same-chain DEX↔DEX pairs.
"""
from __future__ import annotations

from src.scanner.adapters.dex.pool_registry import UNISWAP_V3_FACTORY, UNISWAP_V3_FEE_TIERS
from src.scanner.adapters.dex.v2_adapter import V3DexAdapter


class UniswapV3Adapter(V3DexAdapter):
    display_name = "Uniswap V3"

    def __init__(self, settings, config, sink, network: str, **kw) -> None:
        super().__init__(
            settings, config, sink, network, venue_id=f"uniswap_v3_{network}",
            factory=UNISWAP_V3_FACTORY.get(network), fee_tiers=UNISWAP_V3_FEE_TIERS, **kw)
