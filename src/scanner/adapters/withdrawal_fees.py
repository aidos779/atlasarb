"""Withdrawal-fee estimation (§8.4) — provider abstraction with static fallback.

CEX withdrawal fees are charged as a fixed amount of the withdrawn asset and are only
exposed through *authenticated* endpoints (Binance /sapi/v1/capital/config/getall,
OKX /api/v5/asset/currencies, …) — there is no official unauthenticated source, so the
scanner ships a conservative static table (ARCH-2: no user keys required to run).

The provider is pluggable: a future authenticated `WithdrawalFeeSource` (per venue,
with the operator's read-only key) can be registered and it takes precedence, with the
static table as the always-present fallback. Existing behavior is unchanged until such a
source is registered — the estimate can only get *more* accurate, never break.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

# Per-coin conservative withdrawal cost in USD, over the coin's cheapest common network.
# More specific than a per-network flat: a BTC withdrawal and a SOL withdrawal off the
# same venue differ by orders of magnitude. Values are deliberately on the high side so
# the profit gate never passes a signal a real withdrawal would have made unprofitable.
_STATIC_COIN_FEE_USD: dict[str, Decimal] = {
    "BTC": Decimal("2.0"), "ETH": Decimal("3.0"), "BNB": Decimal("0.20"),
    "SOL": Decimal("0.02"), "USDT": Decimal("1.0"),
    "XRP": Decimal("0.02"), "DOGE": Decimal("0.05"), "LTC": Decimal("0.02"),
    "ADA": Decimal("0.20"), "DOT": Decimal("0.10"), "MATIC": Decimal("0.10"),
    "LINK": Decimal("1.0"), "UNI": Decimal("1.0"), "AAVE": Decimal("1.0"),
    "ARB": Decimal("0.10"), "OP": Decimal("0.10"), "AVAX": Decimal("0.05"),
    "TRX": Decimal("1.0"), "SHIB": Decimal("1.0"), "PEPE": Decimal("1.0"),
}

# Fallback by settlement network when the coin is unknown (legacy §8.4 defaults).
_STATIC_NETWORK_FEE_USD: dict[str | None, Decimal] = {
    "ethereum": Decimal("4.0"), "bnb": Decimal("0.5"), "arbitrum": Decimal("0.5"),
    "optimism": Decimal("0.5"), "base": Decimal("0.5"), "polygon": Decimal("0.3"),
    "solana": Decimal("0.2"), None: Decimal("1.0"),
}


class WithdrawalFeeSource(Protocol):
    """A venue-specific fee source (e.g. an authenticated exchange endpoint).

    Returns the withdrawal fee in USD for (base_asset, network), or None if it cannot
    resolve it (unknown coin/network, endpoint error) — the provider then falls back.
    """

    def fee_usd(self, venue: str, base_asset: str,
                network: str | None) -> Decimal | None: ...


class WithdrawalFeeProvider:
    """Resolves withdrawal fees, preferring registered live sources over the static
    table. One shared instance is injected into every CEX adapter."""

    def __init__(self) -> None:
        self._sources: dict[str, WithdrawalFeeSource] = {}

    def register_source(self, venue: str, source: WithdrawalFeeSource) -> None:
        """Attach a live (typically authenticated) fee source for one venue."""
        self._sources[venue] = source

    def fee_usd(self, venue: str, base_asset: str, network: str | None) -> Decimal:
        source = self._sources.get(venue)
        if source is not None:
            live = source.fee_usd(venue, base_asset, network)
            if live is not None:
                return live
        coin = _STATIC_COIN_FEE_USD.get(base_asset.upper())
        if coin is not None:
            return coin
        return _STATIC_NETWORK_FEE_USD.get(network, _STATIC_NETWORK_FEE_USD[None])
