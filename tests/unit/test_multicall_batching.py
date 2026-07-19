"""Multicall3 batching (P1.3): collapse many per-pool eth_calls into one request.

Covers the hand-rolled ABI codec (round-tripped against independent mirror
encoders/decoders written straight from the ABI spec), multicall_read's per-call result
mapping, and the adapter read_pools path — including the safety fallback to per-pool reads
when the multicall is unavailable so batching never becomes a correctness dependency.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.config.scanner_config import ScannerConfig
from src.scanner.adapters.dex.multicall import (
    MULTICALL3_ADDRESS,
    decode_aggregate3,
    encode_aggregate3,
    multicall_read,
)
from src.scanner.adapters.dex.pool_registry import PoolDef
from src.scanner.adapters.dex.v2_adapter import V3DexAdapter


# ── independent mirror codecs (written from the ABI spec, not reusing the impl) ──
def _w(n: int) -> bytes:
    return n.to_bytes(32, "big")


def _decode_aggregate3_args(hex_calldata: str) -> list[tuple[str, bool, bytes]]:
    """Decode the aggregate3 ARG type (address,bool,bytes)[] — validates encode_aggregate3."""
    raw = bytes.fromhex(hex_calldata[10:])  # strip 0x + 4-byte selector
    arr_off = int.from_bytes(raw[0:32], "big")
    n = int.from_bytes(raw[arr_off:arr_off + 32], "big")
    base = arr_off + 32
    out = []
    for i in range(n):
        rel = int.from_bytes(raw[base + 32 * i:base + 32 * i + 32], "big")
        tup = base + rel
        target = "0x" + raw[tup + 12:tup + 32].hex()
        allow = int.from_bytes(raw[tup + 32:tup + 64], "big") != 0
        boff = int.from_bytes(raw[tup + 64:tup + 96], "big")
        blen = int.from_bytes(raw[tup + boff:tup + boff + 32], "big")
        data = raw[tup + boff + 32:tup + boff + 32 + blen]
        out.append((target, allow, data))
    return out


def _encode_results(results: list[tuple[bool, bytes]]) -> str:
    """Encode the aggregate3 RETURN type (bool,bytes)[] — validates decode_aggregate3."""
    tuples = []
    for success, data in results:
        pad = (-len(data)) % 32
        tuples.append(_w(1 if success else 0) + _w(0x40) + _w(len(data)) + data + b"\x00" * pad)
    n = len(tuples)
    head = b""
    tail = b""
    offset = 32 * n
    for enc in tuples:
        head += _w(offset)
        offset += len(enc)
        tail += enc
    return "0x" + (_w(0x20) + _w(n) + head + tail).hex()


def test_encode_selector_and_roundtrip_args():
    calls = [("0x0000000000000000000000000000000000000001", "0x0902f1ac"),
             ("0x00000000000000000000000000000000000000aB", "0x3850c7bd11223344")]
    encoded = encode_aggregate3(calls)
    assert encoded.startswith("0x82ad56cb")  # aggregate3 selector
    decoded = _decode_aggregate3_args(encoded)
    assert [(t, a) for t, a, _ in decoded] == [
        ("0x0000000000000000000000000000000000000001", True),
        ("0x00000000000000000000000000000000000000ab", True),
    ]
    assert decoded[0][2] == bytes.fromhex("0902f1ac")
    assert decoded[1][2] == bytes.fromhex("3850c7bd11223344")


def test_decode_aggregate3_recovers_results():
    payload = [(True, bytes.fromhex("de") * 32), (False, b""), (True, bytes.fromhex("beef"))]
    decoded = decode_aggregate3(_encode_results(payload))
    assert decoded is not None
    assert decoded[0] == (True, "0x" + "de" * 32)
    assert decoded[1] == (False, "0x")
    assert decoded[2] == (True, "0xbeef")


def test_decode_aggregate3_rejects_garbage():
    assert decode_aggregate3("0x1234") is None
    assert decode_aggregate3("0x") is None


class _MulticallAdapter:
    """Minimal stand-in exposing the surface multicall_read touches."""
    network = "ethereum"

    def __init__(self, eth_call_return):
        self._ret = eth_call_return
        self.calls: list[tuple[str, str]] = []

    async def eth_call(self, to, data):
        self.calls.append((to, data))
        return self._ret


@pytest.mark.asyncio
async def test_multicall_read_maps_success_and_failure_slots():
    ret = _encode_results([(True, bytes.fromhex("aa")), (False, b"")])
    adapter = _MulticallAdapter(ret)
    out = await multicall_read(adapter, [
        ("0x0000000000000000000000000000000000000011", "0x01"),
        ("0x0000000000000000000000000000000000000022", "0x02"),
    ])
    assert out == ["0xaa", None]          # reverted slot → None
    assert len(adapter.calls) == 1        # exactly ONE rpc request for both sub-calls
    assert adapter.calls[0][0] == MULTICALL3_ADDRESS


@pytest.mark.asyncio
async def test_multicall_read_returns_none_when_call_fails():
    adapter = _MulticallAdapter(None)  # multicall itself failed (all providers down)
    assert await multicall_read(
        adapter, [("0x0000000000000000000000000000000000000011", "0x01")]) is None


# ── adapter read_pools: batched success + per-pool fallback ──
_V3_POOL = PoolDef(
    base_asset="WETH", quote_asset="USDT",
    pool_address="0x0000000000000000000000000000000000000010",
    token0_is_base=True, base_decimals=18, quote_decimals=6, fee_tier=Decimal("0.0005"),
)

# A sqrtPriceX96 / liquidity pair that clears the reader's dust guards.
_SQRT_PRICE_X96 = 2 ** 96          # price 1.0 in token1/token0 terms
_LIQUIDITY = 10 ** 24


def _v3_adapter():
    settings = SimpleNamespace(
        rpc_urls_for=lambda net: ["u1"],
        rpc_primary_urls_for=lambda net: set(),
    )
    return V3DexAdapter(settings, ScannerConfig(), sink=SimpleNamespace(),
                        network="ethereum", venue_id="uniswap_v3_ethereum",
                        factory=None, fee_tiers=(500,), fallback_pools=[_V3_POOL])


@pytest.mark.asyncio
async def test_read_pools_batches_into_single_multicall():
    slot0 = "0x" + format(_SQRT_PRICE_X96, "064x") + "0" * (64 * 6)  # sqrtPriceX96 + tail
    liq = "0x" + format(_LIQUIDITY, "064x")
    multicall_ret = _encode_results([(True, bytes.fromhex(slot0[2:])),
                                     (True, bytes.fromhex(liq[2:]))])
    adapter = _v3_adapter()
    seen = []

    async def fake_eth_call(to, data):
        seen.append(to)
        return multicall_ret

    adapter.eth_call = fake_eth_call
    books = await adapter.read_pools(
        [type("S", (), {"pair": "WETH/USDT"})()]  # object with .pair
    )
    assert len(books) == 1
    assert books[0].venue == "uniswap_v3_ethereum"
    # Both slot0 and liquidity were served by ONE Multicall3 request.
    assert seen == [MULTICALL3_ADDRESS]


@pytest.mark.asyncio
async def test_read_pools_falls_back_to_per_pool_when_multicall_unavailable():
    """If the multicall returns None (e.g. contract missing / all providers down), the
    adapter must still read each pool individually rather than dropping the data."""
    adapter = _v3_adapter()
    slot0 = "0x" + format(_SQRT_PRICE_X96, "064x") + "0" * (64 * 6)
    liq = "0x" + format(_LIQUIDITY, "064x")
    calls = []

    async def fake_eth_call(to, data):
        calls.append(to)
        if to == MULTICALL3_ADDRESS:
            return None                      # multicall path unavailable
        # per-pool fallback: slot0 selector 0x3850c7bd, liquidity 0x1a686502
        return slot0 if "3850c7bd" in data else liq

    adapter.eth_call = fake_eth_call
    books = await adapter.read_pools([type("S", (), {"pair": "WETH/USDT"})()])
    assert len(books) == 1
    assert MULTICALL3_ADDRESS in calls          # tried the batch first
    # Fallback then issued the two direct pool calls (slot0 + liquidity).
    assert calls.count(_V3_POOL.pool_address) == 2
