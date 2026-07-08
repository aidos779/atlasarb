"""Bybit v5 adapter — official REST instruments + WS tickers/orderbook (ARCH-2)."""
from __future__ import annotations

from decimal import Decimal

import aiohttp

from src.domain.market import BookLevel, CanonicalSymbol, FundingRate, OrderBook, PriceQuote
from src.scanner.adapters.base_cex import BaseCexAdapter


class BybitAdapter(BaseCexAdapter):
    id = "bybit"
    display_name = "Bybit"
    ws_max_symbols_per_conn = 80
    ws_max_conns = 3

    def __init__(self, settings, config, sink, **kw) -> None:
        super().__init__(settings, config, sink, settings.bybit_rest_url,
                         settings.bybit_ws_url, rate_per_sec=20, burst=40, **kw)
        self._last_book: dict[str, tuple[list, list]] = {}

    def _ping_path(self) -> str:
        return "/v5/market/time"

    def _raw_symbol(self, symbol: CanonicalSymbol) -> str:
        return f"{symbol.base_asset}{symbol.quote_asset}"

    async def _fetch_markets(self, session: aiohttp.ClientSession) -> list[CanonicalSymbol]:
        async with session.get(f"{self._rest_url}/v5/market/instruments-info",
                               params={"category": "spot"}) as resp:
            data = await resp.json()
        out = []
        for s in data.get("result", {}).get("list", []):
            if s.get("status") != "Trading":
                continue
            quote = s.get("quoteCoin")
            if quote not in ("USDT", "USDC"):
                continue
            out.append(self._canonical(s["baseCoin"], quote))
        return out

    def _heartbeat_frame(self) -> dict | str | None:
        return {"op": "ping"}

    def _subscribe_frames(self, symbols: list[CanonicalSymbol]) -> list[dict]:
        args = []
        for s in symbols:
            raw = self._raw_symbol(s)
            args.append(f"tickers.{raw}")
            args.append(f"orderbook.50.{raw}")
        frames = []
        for i in range(0, len(args), 10):
            frames.append({"op": "subscribe", "args": args[i:i + 10]})
        return frames

    def _parse_message(self, message: dict) -> list[PriceQuote | OrderBook]:
        topic = message.get("topic", "")
        data = message.get("data")
        if not topic or data is None:
            return []
        if topic.startswith("tickers."):
            raw = topic.split(".", 1)[1]
            sym = self._symbol_map.get(raw)
            if not sym or "bid1Price" not in data:
                return []
            return [PriceQuote(
                venue=self.id, symbol=sym, bid=Decimal(str(data["bid1Price"])),
                ask=Decimal(str(data["ask1Price"])),
                last=Decimal(str(data.get("lastPrice", data["ask1Price"]))), source="WS",
            )]
        if topic.startswith("orderbook."):
            raw = topic.rsplit(".", 1)[1]
            sym = self._symbol_map.get(raw)
            if not sym:
                return []
            bids = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in data.get("b", [])]
            asks = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in data.get("a", [])]
            # Bybit sends snapshot/delta; keep last non-empty side on delta.
            prev = self._last_book.get(raw, ([], []))
            if not bids:
                bids = prev[0]
            if not asks:
                asks = prev[1]
            self._last_book[raw] = (bids, asks)
            if not bids or not asks:
                return []
            return [OrderBook(venue=self.id, symbol=sym, bids=bids, asks=asks,
                              sequence=data.get("seq"))]
        return []

    async def _fetch_funding(self, session, base_asset: str) -> FundingRate | None:
        async with session.get(f"{self._rest_url}/v5/market/tickers",
                               params={"category": "linear", "symbol": f"{base_asset}USDT"}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        rows = data.get("result", {}).get("list", [])
        if not rows:
            return None
        d = rows[0]
        return FundingRate(
            venue=self.id, base_asset=base_asset,
            current_rate=Decimal(str(d.get("fundingRate", "0"))), predicted_rate=None,
            next_funding_time=float(d.get("nextFundingTime", 0)) / 1000.0, interval_hours=8,
        )
