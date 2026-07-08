"""Minimal protobuf wire decoder for MEXC's PushDataV3ApiWrapper frames.

MEXC discontinued its JSON WS channels (the server answers subscriptions with
"Blocked!") — the current wbs-api.mexc.com endpoint pushes protobuf only. The wrapper
schema (mexcdevelop/websocket-proto) is tiny and stable, so we decode the two public
messages we consume by hand instead of adding a protobuf codegen dependency:

    PushDataV3ApiWrapper:
        1: channel (string)          3: symbol (string)
        303: PublicLimitDepthsV3Api  315: PublicAggreBookTickerV3Api

    PublicLimitDepthsV3Api:  1: asks[] {1: price, 2: quantity}   2: bids[] {...}
    PublicAggreBookTickerV3Api: 1: bidPrice 2: bidQuantity 3: askPrice 4: askQuantity

Field layout verified against live frames (see git history / runtime probe).
"""
from __future__ import annotations


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def parse_fields(data: bytes) -> list[tuple[int, int, object]]:
    """Decode one message level into (field_number, wire_type, value) tuples.
    Length-delimited values are returned as raw bytes; caller recurses as needed."""
    out: list[tuple[int, int, object]] = []
    i = 0
    n = len(data)
    while i < n:
        tag, i = _read_varint(data, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, i = _read_varint(data, i)
        elif wire == 2:
            length, i = _read_varint(data, i)
            value = data[i:i + length]
            i += length
        elif wire == 5:
            value = data[i:i + 4]
            i += 4
        elif wire == 1:
            value = data[i:i + 8]
            i += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((field, wire, value))
    return out


def _levels(items: list[tuple[int, int, object]], side_field: int) -> list[tuple[str, str]]:
    levels: list[tuple[str, str]] = []
    for field, wire, value in items:
        if field != side_field or wire != 2:
            continue
        price = qty = None
        for f, w, v in parse_fields(value):  # type: ignore[arg-type]
            if w != 2:
                continue
            if f == 1:
                price = v.decode()  # type: ignore[union-attr]
            elif f == 2:
                qty = v.decode()  # type: ignore[union-attr]
        if price is not None and qty is not None:
            levels.append((price, qty))
    return levels


def decode_push(data: bytes) -> dict | None:
    """Decode a wrapper frame to {'channel', 'symbol', and either 'book_ticker' or
    'depth'} — or None when the frame carries neither message we consume."""
    channel = symbol = None
    ticker = depth = None
    for field, wire, value in parse_fields(data):
        if wire != 2:
            continue
        if field == 1:
            channel = value.decode()  # type: ignore[union-attr]
        elif field == 3:
            symbol = value.decode()  # type: ignore[union-attr]
        elif field == 315:
            inner = dict()
            for f, w, v in parse_fields(value):  # type: ignore[arg-type]
                if w == 2 and f in (1, 2, 3, 4):
                    inner[f] = v.decode()  # type: ignore[union-attr]
            if all(k in inner for k in (1, 2, 3, 4)):
                ticker = {"bid": inner[1], "bid_qty": inner[2],
                          "ask": inner[3], "ask_qty": inner[4]}
        elif field == 303:
            items = parse_fields(value)  # type: ignore[arg-type]
            depth = {"asks": _levels(items, 1), "bids": _levels(items, 2)}
    if channel is None or symbol is None or (ticker is None and depth is None):
        return None
    return {"channel": channel, "symbol": symbol,
            "book_ticker": ticker, "depth": depth}
