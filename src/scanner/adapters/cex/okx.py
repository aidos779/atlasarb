"""OKX v5 adapter — official REST instruments + WS books5 (ARCH-2)."""
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


class OkxAdapter(BaseCexAdapter):
    id = "okx"
    display_name = "OKX"
    ws_max_symbols_per_conn = 80
    ws_max_conns = 3

    def __init__(self, settings, config, sink, **kw) -> None:
        super().__init__(settings, config, sink, settings.okx_rest_url,
                         settings.okx_ws_url, rate_per_sec=20, burst=40, **kw)

    def _ping_path(self) -> str:
        return "/api/v5/public/time"

    def _raw_symbol(self, symbol: CanonicalSymbol) -> str:
        return f"{symbol.base_asset}-{symbol.quote_asset}"

    def _raw_symbol_from_pair(self, pair: str) -> str:
        base, _, quote = pair.partition("/")
        return f"{base}-{quote}"

    async def _fetch_markets(self, session: aiohttp.ClientSession) -> list[CanonicalSymbol]:
        data = await self._get_json_logged(
            session, f"{self._rest_url}/api/v5/public/instruments",
            params={"instType": "SPOT"}, timeout_sec=self._config.cex_discovery_timeout_sec)
        out = []
        for s in data.get("data", []):
            if s.get("state") != "live":
                continue
            quote = s.get("quoteCcy")
            if not is_supported_quote(quote or ""):
                continue
            out.append(self._canonical(s["baseCcy"], quote))
        return out

    def _heartbeat_frame(self) -> dict | str | None:
        return "ping"

    def _subscribe_frames(self, symbols: list[CanonicalSymbol]) -> list[dict]:
        args = []
        for s in symbols:
            inst = self._raw_symbol(s)
            args.append({"channel": "bbo-tbt", "instId": inst})
            args.append({"channel": "books5", "instId": inst})
        frames = []
        for i in range(0, len(args), 50):
            frames.append({"op": "subscribe", "args": args[i:i + 50]})
        return frames

    def _parse_message(self, message: dict) -> list[PriceQuote | OrderBook]:
        arg = message.get("arg", {})
        channel = arg.get("channel")
        inst = arg.get("instId")
        rows = message.get("data")
        if not channel or not inst or not rows:
            return []
        sym = self._symbol_map.get(inst)
        if not sym:
            return []
        d = rows[0]
        bids = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q, *_ in d.get("bids", [])]
        asks = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q, *_ in d.get("asks", [])]
        if channel == "bbo-tbt":
            if not bids or not asks:
                return []
            return [PriceQuote(venue=self.id, symbol=sym, bid=bids[0].price,
                               ask=asks[0].price, last=asks[0].price, source="WS")]
        if channel == "books5":
            if not bids or not asks:
                return []
            return [OrderBook(venue=self.id, symbol=sym, bids=bids, asks=asks)]
        return []

    async def _fetch_funding(self, session, base_asset: str) -> FundingRate | None:
        inst = f"{base_asset}-USDT-SWAP"
        async with session.get(f"{self._rest_url}/api/v5/public/funding-rate",
                               params={"instId": inst}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        rows = data.get("data", [])
        if not rows:
            return None
        d = rows[0]
        return FundingRate(
            venue=self.id, base_asset=base_asset,
            current_rate=Decimal(str(d.get("fundingRate", "0"))), predicted_rate=None,
            next_funding_time=float(d.get("nextFundingTime", 0)) / 1000.0, interval_hours=8,
        )
