"""MEXC v3 adapter — official REST exchangeInfo + protobuf WS bookTicker/depth (ARCH-2).

MEXC's JSON WS channels are discontinued (server replies "Blocked!"); the current
wbs-api.mexc.com endpoint streams protobuf only, decoded in mexc_pb."""
from __future__ import annotations

from decimal import Decimal

import aiohttp

from src.domain.market import (
    BookLevel,
    CanonicalSymbol,
    FundingRate,
    OrderBook,
    PriceQuote,
    is_supported_quote,
)
from src.scanner.adapters.base_cex import BaseCexAdapter
from src.scanner.adapters.cex.mexc_pb import decode_push

_FUTURES_URL = "https://contract.mexc.com"


class MexcAdapter(BaseCexAdapter):
    id = "mexc"
    display_name = "MEXC"
    # 30 subscriptions per connection (2 channels per symbol) per MEXC WS limits.
    ws_max_symbols_per_conn = 15
    ws_max_conns = 10

    def __init__(self, settings, config, sink, **kw) -> None:
        super().__init__(settings, config, sink, settings.mexc_rest_url,
                         settings.mexc_ws_url, rate_per_sec=20, burst=40, **kw)

    def _ping_path(self) -> str:
        return "/api/v3/ping"

    def _raw_symbol(self, symbol: CanonicalSymbol) -> str:
        return f"{symbol.base_asset}{symbol.quote_asset}"

    async def _fetch_markets(self, session: aiohttp.ClientSession) -> list[CanonicalSymbol]:
        data = await self._get_json_logged(session, f"{self._rest_url}/api/v3/exchangeInfo",
                                           timeout_sec=self._config.cex_discovery_timeout_sec)
        out = []
        for s in data.get("symbols", []):
            status = s.get("status")
            if status not in ("ENABLED", "1", 1, "TRADING"):
                continue
            quote = s.get("quoteAsset")
            if not is_supported_quote(quote or ""):
                continue
            out.append(self._canonical(s["baseAsset"], quote))
        return out

    def _heartbeat_frame(self) -> dict | str | None:
        return {"method": "PING"}

    def _subscribe_frames(self, symbols: list[CanonicalSymbol]) -> list[dict]:
        # Only the .pb (protobuf) channels are served today — the legacy JSON
        # channels answer "Not Subscribed successfully! ... Reason: Blocked!".
        params = []
        for s in symbols:
            raw = self._raw_symbol(s)
            params.append(f"spot@public.aggre.bookTicker.v3.api.pb@100ms@{raw}")
            params.append(f"spot@public.limit.depth.v3.api.pb@{raw}@5")
        frames = []
        for i in range(0, len(params), 20):
            frames.append({"method": "SUBSCRIPTION", "params": params[i:i + 20]})
        return frames

    def _parse_message(self, message: dict) -> list[PriceQuote | OrderBook]:
        return []  # market data arrives on the binary (protobuf) path only

    def _parse_binary(self, data: bytes) -> list[PriceQuote | OrderBook]:
        push = decode_push(data)
        if push is None:
            return []
        sym = self._symbol_map.get(push["symbol"])
        if not sym:
            return []
        ticker = push["book_ticker"]
        if ticker is not None:
            return [PriceQuote(
                venue=self.id, symbol=sym, bid=Decimal(ticker["bid"]),
                ask=Decimal(ticker["ask"]), last=Decimal(ticker["ask"]), source="WS")]
        depth = push["depth"]
        if depth is not None:
            bids = [BookLevel(Decimal(p), Decimal(q)) for p, q in depth["bids"]]
            asks = [BookLevel(Decimal(p), Decimal(q)) for p, q in depth["asks"]]
            if not bids or not asks:
                return []
            return [OrderBook(venue=self.id, symbol=sym, bids=bids, asks=asks)]
        return []

    async def _fetch_funding(self, session, base_asset: str) -> FundingRate | None:
        symbol = f"{base_asset}_USDT"
        async with session.get(f"{_FUTURES_URL}/api/v1/contract/funding_rate/{symbol}") as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        d = data.get("data")
        if not d:
            return None
        return FundingRate(
            venue=self.id, base_asset=base_asset,
            current_rate=Decimal(str(d.get("fundingRate", "0"))), predicted_rate=None,
            next_funding_time=float(d.get("nextSettleTime", 0)) / 1000.0,
            interval_hours=int(d.get("collectCycle", 8)),
        )
