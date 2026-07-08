"""Uniswap-V3-style pool reader — slot0()+liquidity() decoded to virtual reserves.

A concentrated-liquidity pool has no single global reserve pair, but around its current
price the active in-range liquidity L and current sqrt-price behave exactly like a
constant-product pool with *virtual* reserves:

    reserve0 = L * 2**96 / sqrtPriceX96      reserve1 = L * sqrtPriceX96 / 2**96

Those virtual reserves reproduce both the marginal price and the local slippage curve,
so they feed the existing DexPoolLeg / ProfitEngine unchanged (§5.4). The approximation
holds while a trade stays within the current tick range — the regime arbitrage sizing
operates in; larger fills that cross many ticks are out of MVP scope, same class of
documented approximation as the V2 model and the Jupiter synthesized-reserve leg.
"""
from __future__ import annotations

from decimal import Decimal

from src.domain.enums import VenueType
from src.domain.market import CanonicalSymbol, OrderBook
from src.scanner.adapters.base_dex import BaseDexAdapter
from src.scanner.adapters.dex.pool_registry import PoolDef

_SLOT0_SELECTOR = "0x3850c7bd"      # keccak("slot0()")[:4]
_LIQUIDITY_SELECTOR = "0x1a686502"  # keccak("liquidity()")[:4]
_Q96 = 1 << 96


async def read_v3_pool(adapter: BaseDexAdapter, venue: str, network: str,
                       pool: PoolDef) -> OrderBook | None:
    slot0 = await adapter.eth_call(pool.pool_address, _SLOT0_SELECTOR)
    if slot0 is None:
        return None
    raw = slot0[2:] if slot0.startswith("0x") else slot0
    if len(raw) < 64:
        return None
    sqrt_price_x96 = int(raw[0:64], 16)  # first return word = sqrtPriceX96
    if sqrt_price_x96 <= 0:
        return None

    liq = await adapter.eth_call(pool.pool_address, _LIQUIDITY_SELECTOR)
    if liq is None:
        return None
    liquidity = int(liq, 16)
    if liquidity <= 0:
        return None  # no in-range liquidity — pool is untradeable right now

    # Virtual reserves in raw token units.
    reserve0_raw = liquidity * _Q96 // sqrt_price_x96
    reserve1_raw = liquidity * sqrt_price_x96 // _Q96

    if pool.token0_is_base:
        base_raw, quote_raw = reserve0_raw, reserve1_raw
    else:
        base_raw, quote_raw = reserve1_raw, reserve0_raw

    reserve_base = Decimal(base_raw) / (Decimal(10) ** pool.base_decimals)
    reserve_quote = Decimal(quote_raw) / (Decimal(10) ** pool.quote_decimals)
    if reserve_base <= 0 or reserve_quote <= 0:
        return None
    # Quote side is a stablecoin, so reserve_quote ≈ in-range USD depth; skip pools
    # too thin to price sanely (mirrors the V2 dust-pool guard).
    if reserve_quote < Decimal(1000):
        return None

    symbol = CanonicalSymbol(pool.base_asset, pool.quote_asset, VenueType.DEX, network)
    return OrderBook(
        venue=venue, symbol=symbol, bids=[], asks=[],
        pool_address=pool.pool_address, pool_fee_tier=pool.fee_tier,
        reserve_base=reserve_base, reserve_quote=reserve_quote,
    )
