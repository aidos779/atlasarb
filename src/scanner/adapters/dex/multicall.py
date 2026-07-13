"""Multicall3 batching for EVM pool reads (Scanner §2.4 RPC-load reduction).

Every DEX poll cycle previously issued one `eth_call` per pool (V2: getReserves; V3:
slot0 + liquidity) — dozens to hundreds of individual JSON-RPC requests per network per
cycle, which is what saturated the public-node rate limits and drove the
`rpc_all_providers_failed` storms. Multicall3 collapses a whole batch of those reads into
a **single** `eth_call` to the canonical Multicall3 contract, which is deployed at the same
address on every EVM network we scan (Ethereum, BNB, Arbitrum, Optimism, Base, Polygon).

We hand-roll the tiny slice of ABI codec we need (`aggregate3` in, `(bool,bytes)[]` out) —
same approach as the existing readers/discovery — to avoid pulling in web3/eth-abi. Any
decode failure or a reverted multicall returns None so the caller can fall back to
per-pool reads: batching is a throughput optimization, never a correctness dependency.
"""
from __future__ import annotations

from src.config import get_logger
from src.scanner.adapters.base_dex import BaseDexAdapter

log = get_logger("adapter.dex.multicall")

# Canonical Multicall3 deployment — identical address on all EVM chains we support.
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
# aggregate3((address target, bool allowFailure, bytes callData)[]) selector.
_AGGREGATE3_SELECTOR = "0x82ad56cb"

# Max sub-calls per multicall. Bounds the single request's calldata / return size and gas
# so one batch can't exceed a public node's response limits; batches beyond this are split.
DEFAULT_MAX_CALLS_PER_BATCH = 60


def _word(n: int) -> bytes:
    return n.to_bytes(32, "big")


def _addr_word(addr: str) -> bytes:
    return bytes.fromhex(addr.lower().removeprefix("0x").rjust(64, "0"))


def _bytes_field(data: bytes) -> bytes:
    """Length-prefixed, 32-byte-padded dynamic `bytes` encoding."""
    pad = (-len(data)) % 32
    return _word(len(data)) + data + b"\x00" * pad


def encode_aggregate3(calls: list[tuple[str, str]], *, allow_failure: bool = True) -> str:
    """Encode calldata for aggregate3 over ``[(target, calldata_hex), ...]``.

    Returns a 0x-prefixed hex string. ``allowFailure=True`` so one reverting pool never
    fails the whole batch — its slot just comes back ``success=False``.
    """
    flag = _word(1 if allow_failure else 0)
    tuples: list[bytes] = []
    for target, calldata in calls:
        data = bytes.fromhex(calldata.removeprefix("0x"))
        # tuple head: target, allowFailure, offset-to-bytes (always 0x60 = 3 words in).
        tuples.append(_addr_word(target) + flag + _word(0x60) + _bytes_field(data))

    n = len(tuples)
    head = b""
    tail = b""
    offset = 32 * n  # tuple-offsets are relative to the start of the head block
    for enc in tuples:
        head += _word(offset)
        offset += len(enc)
        tail += enc
    # arg is a single dynamic array → its offset (0x20) then len, heads, tails.
    encoded = _word(0x20) + _word(n) + head + tail
    return _AGGREGATE3_SELECTOR + encoded.hex()


def decode_aggregate3(result_hex: str) -> list[tuple[bool, str]] | None:
    """Decode an aggregate3 return (``(bool success, bytes returnData)[]``).

    Returns ``[(success, returndata_hex), ...]`` or None if the bytes don't parse (a
    reverted multicall, a truncated response) — the caller then falls back to per-pool.
    """
    try:
        raw = bytes.fromhex(result_hex.removeprefix("0x"))
        if len(raw) < 64:
            return None
        arr_off = int.from_bytes(raw[0:32], "big")
        n = int.from_bytes(raw[arr_off:arr_off + 32], "big")
        base = arr_off + 32  # start of the per-element head slots
        out: list[tuple[bool, str]] = []
        for i in range(n):
            rel = int.from_bytes(raw[base + 32 * i:base + 32 * i + 32], "big")
            tup = base + rel
            success = int.from_bytes(raw[tup:tup + 32], "big") != 0
            bytes_off = int.from_bytes(raw[tup + 32:tup + 64], "big")
            blen_pos = tup + bytes_off
            blen = int.from_bytes(raw[blen_pos:blen_pos + 32], "big")
            data = raw[blen_pos + 32:blen_pos + 32 + blen]
            out.append((success, "0x" + data.hex()))
        if len(out) != n:
            return None
        return out
    except (ValueError, IndexError):
        return None


async def multicall_read(adapter: BaseDexAdapter,
                         calls: list[tuple[str, str]]) -> list[str | None] | None:
    """Run ``calls`` in one Multicall3 ``eth_call`` and return per-call result hex.

    ``None`` in a slot = that sub-call reverted/returned empty. ``None`` for the whole
    result = the multicall itself failed to execute or decode — caller falls back to
    issuing the calls individually.
    """
    if not calls:
        return []
    data = encode_aggregate3(calls)
    result = await adapter.eth_call(MULTICALL3_ADDRESS, data)
    if result is None:
        return None
    decoded = decode_aggregate3(result)
    if decoded is None or len(decoded) != len(calls):
        log.debug("multicall_decode_failed", network=adapter.network, calls=len(calls))
        return None
    return [ret if success and ret not in ("0x", "") else None
            for success, ret in decoded]
