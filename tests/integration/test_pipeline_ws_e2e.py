"""Full pipeline over real WebSockets: two CEX adapters stream arbitrageable
prices through the real ScanningEngine and must produce a published signal.

This is the end-to-end guard for the "no candidates / no signals after discovery"
report: discovery runs AFTER connect, pairs are live-subscribed, data flows into
the cache, detectors fire, and a signal is admitted.
"""
import asyncio
from decimal import Decimal

import aiohttp
import orjson
import pytest
from aiohttp import web

from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook, PriceQuote
from src.scanner.adapters.base_cex import BaseCexAdapter
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.engine import ScanningEngine
from src.scanner.status.health_registry import HealthRegistry

pytestmark = pytest.mark.asyncio

SYM = CanonicalSymbol("ETH", "USDT", VenueType.CEX)


class _Adapter(BaseCexAdapter):
    async def _fetch_markets(self, session):
        return [SYM]

    def _subscribe_frames(self, symbols):
        return [{"op": "subscribe", "pairs": [self._raw_symbol(s) for s in symbols]}]

    def _parse_message(self, message):
        sym = self._symbol_map.get(message.get("s", ""))
        if not sym:
            return []
        if message.get("type") == "ticker":
            return [PriceQuote(venue=self.id, symbol=sym, bid=Decimal(message["b"]),
                               ask=Decimal(message["a"]), last=Decimal(message["a"]),
                               source="WS")]
        if message.get("type") == "book":
            return [OrderBook(venue=self.id, symbol=sym,
                              bids=[BookLevel(Decimal(message["b"]), Decimal("100"))],
                              asks=[BookLevel(Decimal(message["a"]), Decimal("100"))])]
        return []

    async def _fetch_funding(self, session, base_asset):
        return None

    async def health_check(self):
        return True  # streaming also marks online; keep REST probe green too


async def _server(bid: str, ask: str):
    app = web.Application()

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        subscribed: list[str] = []

        async def streamer():
            while not ws.closed:
                for raw in subscribed:
                    await ws.send_str(orjson.dumps(
                        {"type": "ticker", "s": raw, "b": bid, "a": ask}).decode())
                    await ws.send_str(orjson.dumps(
                        {"type": "book", "s": raw, "b": bid, "a": ask}).decode())
                await asyncio.sleep(0.15)

        task = asyncio.create_task(streamer())
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    frame = orjson.loads(msg.data)
                    if frame.get("op") == "subscribe":
                        subscribed.extend(frame.get("pairs", []))
        finally:
            task.cancel()
        return ws

    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"ws://127.0.0.1:{port}/ws"


class _Queue:
    def __init__(self):
        self.msgs = []

    async def publish(self, signal, event):
        self.msgs.append((event, signal))


class _History:
    async def archive(self, s):
        pass


class _Gas:
    async def gas_price_usd(self, n, u):
        return Decimal("0")


async def test_full_pipeline_produces_signal_over_ws():
    # Buy on cheap venue (ask 3001), sell on rich venue (bid 3050) -> ~1.6% gross.
    runner_a, url_a = await _server(bid="3000", ask="3001")
    runner_b, url_b = await _server(bid="3050", ask="3051")

    cfg = ScannerConfig()
    cfg.ws_ping_interval_sec = 0.2
    cfg.warmup_samples = 3
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)

    def hooks(vid):
        def on_success(latency=0.0, stream=False):
            health.record_success(vid, latency, stream=stream)

        def on_failure(hard=False):
            health.record_failure(vid, hard=hard)

        return on_success, on_failure

    from src.config.settings import Settings
    settings = Settings()
    adapters = {}
    for vid, url in (("cheapex", url_a), ("richex", url_b)):
        s, f = hooks(vid)
        ad = _Adapter(settings, cfg, cache, rest_url="http://x", ws_url=url,
                      rate_per_sec=100, burst=100, on_success=s, on_failure=f)
        ad.id = vid
        ad.display_name = vid
        adapters[vid] = ad

    q = _Queue()
    engine = ScanningEngine(cfg, adapters, q, _History(), _Gas(), {"ETH"},
                            cache=cache, health=health)
    try:
        await engine.start()
        signal = None
        for _ in range(60):
            if q.msgs:
                signal = q.msgs[0][1]
                break
            await asyncio.sleep(0.1)
        assert engine.metrics.snapshots_received > 0, "no data reached the cache"
        assert engine.metrics.candidates_generated > 0, "no candidates created"
        assert q.msgs, "no signal published"
        assert signal.buy_exchange == "cheapex"
        assert signal.sell_exchange == "richex"
        assert signal.net_profit_pct > 0
    finally:
        await engine.stop()
        await runner_a.cleanup()
        await runner_b.cleanup()
