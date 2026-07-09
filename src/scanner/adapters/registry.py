"""Adapter registry / factory (composition of the exchange layer).

Builds all MVP adapters (4 CEX + Uniswap across EVM networks + PancakeSwap + Jupiter)
against the shared cache sink and health registry, wiring each adapter's success/failure
into the Exchange Health Registry (§2.2/§14). Adding a Future Exchange (§3.4) is one new
adapter class + one line here — zero engine changes (ARCH-1 / FR-SIG-09).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.domain.ports import ExchangeAdapter
from src.scanner.adapters.cex.binance import BinanceAdapter
from src.scanner.adapters.cex.bitget import BitgetAdapter
from src.scanner.adapters.cex.mexc import MexcAdapter
from src.scanner.adapters.cex.okx import OkxAdapter
from src.scanner.adapters.dex.jupiter import JupiterAdapter
from src.scanner.adapters.dex.pancakeswap import PancakeSwapAdapter
from src.scanner.adapters.dex.pancakeswap_v3 import PancakeSwapV3Adapter
from src.scanner.adapters.dex.pool_registry import (
    PANCAKE_V3_FACTORY,
    SUSHISWAP_V2_FACTORY,
    UNISWAP_POOLS,
    UNISWAP_V3_FACTORY,
    VERIFIED_TOKENS,
)
from src.scanner.adapters.dex.sushiswap import SushiSwapAdapter
from src.scanner.adapters.dex.uniswap import UniswapAdapter
from src.scanner.adapters.dex.uniswap_v3 import UniswapV3Adapter
from src.scanner.adapters.gas import RpcGasProvider
from src.scanner.adapters.withdrawal_fees import WithdrawalFeeProvider
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.status.health_registry import HealthRegistry

_CEX_CLASSES = [BinanceAdapter, OkxAdapter, BitgetAdapter, MexcAdapter]


@dataclass
class ScannerComponents:
    adapters: dict[str, ExchangeAdapter]
    cache: MarketStateCache
    health: HealthRegistry
    gas: RpcGasProvider
    verified_tokens: set[str]


def build_scanner_components(settings: Settings, config: ScannerConfig) -> ScannerComponents:
    cache = MarketStateCache(config)
    health = HealthRegistry(config)

    def hooks(venue_id: str):
        def on_success(latency: float = 0.0, stream: bool = False) -> None:
            health.record_success(venue_id, latency, stream=stream)

        def on_failure(hard: bool = False) -> None:
            health.record_failure(venue_id, hard=hard)

        return on_success, on_failure

    adapters: dict[str, ExchangeAdapter] = {}
    # One shared withdrawal-fee provider (static table now; an authenticated per-venue
    # source can be registered later without touching adapters).
    withdrawal_fees = WithdrawalFeeProvider()

    for cls in _CEX_CLASSES:
        vid = cls.id
        s, f = hooks(vid)
        adapters[vid] = cls(settings, config, cache, on_success=s, on_failure=f,
                            withdrawal_fees=withdrawal_fees)

    # Uniswap: one adapter per configured EVM network with pools.
    for network in UNISWAP_POOLS:
        vid = f"uniswap_{network}"
        s, f = hooks(vid)
        adapters[vid] = UniswapAdapter(settings, config, cache, network=network,
                                       on_success=s, on_failure=f)

    # SushiSwap: second Ethereum DEX venue — enables same-chain DEX↔DEX.
    for network in SUSHISWAP_V2_FACTORY:
        vid = f"sushiswap_{network}"
        s, f = hooks(vid)
        adapters[vid] = SushiSwapAdapter(settings, config, cache, network=network,
                                         on_success=s, on_failure=f)

    # Uniswap V3: concentrated-liquidity venue per network (real V2↔V3 DEX↔DEX).
    for network in UNISWAP_V3_FACTORY:
        vid = f"uniswap_v3_{network}"
        s, f = hooks(vid)
        adapters[vid] = UniswapV3Adapter(settings, config, cache, network=network,
                                         on_success=s, on_failure=f)

    # PancakeSwap V3: concentrated-liquidity venue on BNB Chain.
    for network in PANCAKE_V3_FACTORY:
        vid = f"pancakeswap_v3_{network}"
        s, f = hooks(vid)
        adapters[vid] = PancakeSwapV3Adapter(settings, config, cache, network=network,
                                             on_success=s, on_failure=f)

    s, f = hooks("pancakeswap_bnb")
    adapters["pancakeswap_bnb"] = PancakeSwapAdapter(settings, config, cache,
                                                     on_success=s, on_failure=f)

    s, f = hooks("jupiter")
    adapters["jupiter"] = JupiterAdapter(settings, config, cache, on_success=s, on_failure=f)

    for vid in adapters:
        health.register(vid)

    gas = RpcGasProvider(settings, cache)
    # Give the gas provider an RPC caller backed by the EVM DEX adapters.
    evm_adapters = {a.network: a for vid, a in adapters.items()
                    if getattr(a, "network", None) and a.venue_type.value == "DEX"}

    async def rpc_caller(network: str, method: str, params: list):
        adapter = evm_adapters.get(network)
        if adapter is None or not hasattr(adapter, "rpc_call"):
            return None
        return await adapter.rpc_call(method, params)

    gas.set_rpc_caller(rpc_caller)

    return ScannerComponents(
        adapters=adapters, cache=cache, health=health, gas=gas,
        verified_tokens=set(VERIFIED_TOKENS),
    )
