"""Gas price provider (Scanner §8.5) — live per-network gas cost in USD.

gasFeeUSD = estimatedGasUnits * currentGasPrice * nativeTokenPriceUSD. Gas price is read
live per network via RPC (EIP-1559 eth_gasPrice); native token USD price comes from the
market cache (the same live CEX prices the engine already collects). Returns None when it
cannot resolve — the profit engine then treats gas as unresolved (§10 Fees gate).
"""
from __future__ import annotations

from decimal import Decimal

from src.config.settings import Settings
from src.domain.market import CanonicalSymbol  # noqa: F401 (used for typing intent)
from src.scanner.cache.market_state_cache import MarketStateCache

_NATIVE_SYMBOL = {
    "ethereum": "ETH", "arbitrum": "ETH", "optimism": "ETH", "base": "ETH",
    "bnb": "BNB", "polygon": "MATIC", "solana": "SOL",
}
# Solana fee model is per-signature lamports, not gas*price; conservative flat USD.
_SOLANA_FLAT_USD = Decimal("0.01")


class RpcGasProvider:
    def __init__(self, settings: Settings, cache: MarketStateCache,
                 rpc_caller=None) -> None:
        self._settings = settings
        self._cache = cache
        # rpc_caller(network, method, params) -> result; injected from a DEX adapter.
        self._rpc_caller = rpc_caller

    def set_rpc_caller(self, caller) -> None:
        self._rpc_caller = caller

    async def gas_price_usd(self, network: str, gas_units: int) -> Decimal | None:
        network = network.lower()
        if network == "solana":
            return _SOLANA_FLAT_USD
        native = _NATIVE_SYMBOL.get(network)
        if native is None:
            return None
        native_price = self._native_price_usd(native)
        if native_price is None:
            return None
        gas_price_wei = await self._gas_price_wei(network)
        if gas_price_wei is None:
            return None
        gas_cost_native = Decimal(gas_units) * gas_price_wei / (Decimal(10) ** 18)
        return gas_cost_native * native_price

    def _native_price_usd(self, native: str) -> Decimal | None:
        for quote in ("USDT", "USDC"):
            pair = f"{native}/{quote}"
            for venue in self._cache.venues_for_pair(pair):
                q = self._cache.get_price(venue, pair)
                if q is not None and q.mid > 0:
                    return q.mid
        return None

    async def _gas_price_wei(self, network: str) -> Decimal | None:
        if self._rpc_caller is None:
            return None
        result = await self._rpc_caller(network, "eth_gasPrice", [])
        if not isinstance(result, str):
            return None
        try:
            return Decimal(int(result, 16))
        except (ValueError, TypeError):
            return None
