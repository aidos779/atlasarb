"""Bridge route registry (Scanner §7.5). Cross-chain opportunities are only generated
when a viable route exists. Routes carry fee/time/capacity used by the profit engine
and the cross-chain ranking penalty. Data-driven — routes are configuration, not code.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BridgeRoute:
    name: str
    src_network: str
    dst_network: str
    flat_fee_usd: Decimal
    percent_fee: Decimal          # fraction, e.g. 0.0005
    time_sec: int
    capacity_usd: Decimal


# Conservative default set of well-known canonical bridges between supported networks.
_DEFAULT_ROUTES: list[BridgeRoute] = [
    BridgeRoute("Across", "ethereum", "arbitrum", Decimal(2), Decimal("0.0006"), 90, Decimal(2_000_000)),
    BridgeRoute("Across", "ethereum", "optimism", Decimal(2), Decimal("0.0006"), 90, Decimal(2_000_000)),
    BridgeRoute("Across", "ethereum", "base", Decimal(2), Decimal("0.0006"), 90, Decimal(2_000_000)),
    BridgeRoute("Stargate", "ethereum", "polygon", Decimal(3), Decimal("0.0006"), 300, Decimal(1_500_000)),
    BridgeRoute("Stargate", "ethereum", "bnb", Decimal(3), Decimal("0.0006"), 300, Decimal(1_500_000)),
    BridgeRoute("Across", "arbitrum", "optimism", Decimal(1), Decimal("0.0005"), 60, Decimal(1_000_000)),
    BridgeRoute("Across", "arbitrum", "base", Decimal(1), Decimal("0.0005"), 60, Decimal(1_000_000)),
    BridgeRoute("Stargate", "polygon", "bnb", Decimal(3), Decimal("0.0006"), 300, Decimal(1_000_000)),
    BridgeRoute("Wormhole", "ethereum", "solana", Decimal(5), Decimal("0.0008"), 900, Decimal(800_000)),
    BridgeRoute("Wormhole", "bnb", "solana", Decimal(5), Decimal("0.0008"), 900, Decimal(600_000)),
]


class BridgeRegistry:
    def __init__(self, routes: list[BridgeRoute] | None = None) -> None:
        self._routes: dict[tuple[str, str], BridgeRoute] = {}
        for route in routes or _DEFAULT_ROUTES:
            # Bidirectional.
            self._routes[(route.src_network, route.dst_network)] = route
            self._routes[(route.dst_network, route.src_network)] = BridgeRoute(
                route.name, route.dst_network, route.src_network,
                route.flat_fee_usd, route.percent_fee, route.time_sec, route.capacity_usd,
            )

    def route(self, src: str, dst: str, base_asset: str) -> BridgeRoute | None:
        return self._routes.get((src, dst))

    def has_route(self, src: str, dst: str) -> bool:
        return (src, dst) in self._routes
