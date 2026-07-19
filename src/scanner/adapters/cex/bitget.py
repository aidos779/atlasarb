"""Bitget v2 adapter — official REST symbols + WS ticker/books5 (ARCH-2)."""
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


class BitgetAdapter(BaseCexAdapter):
    id = "bitget"
    display_name = "Bitget"
    ws_max_symbols_per_conn = 150
    ws_max_conns = 3

    def __init__(self, settings, config, sink, **kw) -> None:
        super().__init__(settings, config, sink, settings.bitget_rest_url,
                         settings.bitget_ws_url, rate_per_sec=15, burst=30, **kw)

    def _ping_path(self) -> str:
        return "/api/v2/public/time"

    def _raw_symbol(self, symbol: CanonicalSymbol) -> str:
        return f"{symbol.base_asset}{symbol.quote_asset}"

    async def _fetch_markets(self, session: aiohttp.ClientSession) -> list[CanonicalSymbol]:
        data = await self._get_json_logged(
            session, f"{self._rest_url}/api/v2/spot/public/symbols",
            timeout_sec=self._config.cex_discovery_timeout_sec)
        out = []
        for s in data.get("data", []):
            if s.get("status") != "online":
                continue
            quote = s.get("quoteCoin")
            if not is_supported_quote(quote or ""):
                continue
            out.append(self._canonical(s["baseCoin"], quote))
        return out

    def _heartbeat_frame(self) -> dict | str | None:
        return "ping"

    def _subscribe_frames(self, symbols: list[CanonicalSymbol]) -> list[dict]:
        args = []
        for s in symbols:
            raw = self._raw_symbol(s)
            args.append({"instType": "SPOT", "channel": "ticker", "instId": raw})
            args.append({"instType": "SPOT", "channel": "books5", "instId": raw})
        frames = []
        for i in range(0, len(args), 50):
            frames.append({"op": "subscribe", "args": args[i:i + 50]})
        return frames

    def _parse_message(self, message: dict) -> list[PriceQuote | OrderBook]:
        arg = message.get("arg", {})
        channel = arg.get("channel")
        raw = arg.get("instId")
        rows = message.get("data")
        if not channel or not raw or not rows:
            return []
        sym = self._symbol_map.get(raw)
        if not sym:
            return []
        d = rows[0]
        if channel == "ticker":
            if "bidPr" not in d:
                return []
            return [PriceQuote(
                venue=self.id, symbol=sym, bid=Decimal(str(d["bidPr"])),
                ask=Decimal(str(d["askPr"])),
                last=Decimal(str(d.get("lastPr", d["askPr"]))), source="WS")]
        if channel == "books5":
            bids = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in d.get("bids", [])]
            asks = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in d.get("asks", [])]
            if not bids or not asks:
                return []
            return [OrderBook(venue=self.id, symbol=sym, bids=bids, asks=asks)]
        return []

    async def _fetch_funding(self, session, base_asset: str) -> FundingRate | None:
        async with session.get(f"{self._rest_url}/api/v2/mix/market/current-fund-rate",
                               params={"symbol": f"{base_asset}USDT",
                                       "productType": "USDT-FUTURES"}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        rows = data.get("data", [])
        if not rows:
            return None
        d = rows[0] if isinstance(rows, list) else rows
        import time
        return FundingRate(
            venue=self.id, base_asset=base_asset,
            current_rate=Decimal(str(d.get("fundingRate", "0"))), predicted_rate=None,
            next_funding_time=time.time() + 8 * 3600, interval_hours=8,
        )
