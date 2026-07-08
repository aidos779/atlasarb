"""Uniswap-V2-style pool reader — getReserves() via eth_call, decoded to human units.

Reserves come back as (uint112 reserve0, uint112 reserve1, uint32 ts) packed in three
32-byte words. We map them to base/quote per the pool's token0 ordering and decimals,
yielding the constant-product reserves the profit engine's DexPoolLeg needs (§5.4).
"""
from __future__ import annotations

from decimal import Decimal

from src.domain.enums import VenueType
from src.domain.market import CanonicalSymbol, OrderBook
from src.scanner.adapters.base_dex import BaseDexAdapter
from src.scanner.adapters.dex.pool_registry import PoolDef

_GET_RESERVES_SELECTOR = "0x0902f1ac"  # keccak("getReserves()")[:4]


def _decode_reserves(hex_result: str) -> tuple[int, int] | None:
    raw = hex_result[2:] if hex_result.startswith("0x") else hex_result
    if len(raw) < 128:
        return None
    reserve0 = int(raw[0:64], 16)
    reserve1 = int(raw[64:128], 16)
    return reserve0, reserve1


async def read_v2_pool(adapter: BaseDexAdapter, venue: str, network: str,
                       pool: PoolDef) -> OrderBook | None:
    result = await adapter.eth_call(pool.pool_address, _GET_RESERVES_SELECTOR)
    if result is None:
        return None
    decoded = _decode_reserves(result)
    if decoded is None:
        return None
    reserve0, reserve1 = decoded
    if reserve0 <= 0 or reserve1 <= 0:
        return None

    if pool.token0_is_base:
        base_raw, quote_raw = reserve0, reserve1
    else:
        base_raw, quote_raw = reserve1, reserve0

    reserve_base = Decimal(base_raw) / (Decimal(10) ** pool.base_decimals)
    reserve_quote = Decimal(quote_raw) / (Decimal(10) ** pool.quote_decimals)
    if reserve_base <= 0 or reserve_quote <= 0:
        return None
    # Dust pools (factory-discovered but drained) quote wildly wrong prices —
    # they only feed IMPLAUSIBLE_SPREAD noise. Quote side is a stablecoin, so
    # reserve_quote approximates USD depth.
    if reserve_quote < Decimal(1000):
        return None

    symbol = CanonicalSymbol(pool.base_asset, pool.quote_asset, VenueType.DEX, network)
    return OrderBook(
        venue=venue, symbol=symbol, bids=[], asks=[],
        pool_address=pool.pool_address, pool_fee_tier=pool.fee_tier,
        reserve_base=reserve_base, reserve_quote=reserve_quote,
    )
