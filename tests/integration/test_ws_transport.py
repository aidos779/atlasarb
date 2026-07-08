"""End-to-end WS transport test against a real in-process aiohttp WS server.

Exercises the rewritten BaseCexAdapter receive loop: app-level heartbeat frames
are actually sent, inbound data marks stream success, and an idle server triggers
a reconnect (instead of the old aiohttp control-frame ServerTimeoutError churn).
"""
import asyncio

import aiohttp
import pytest
from aiohttp import web

from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.scanner.adapters.base_cex import BaseCexAdapter

pytestmark = pytest.mark.asyncio


class _Adapter(BaseCexAdapter):
    id = "fake"
    display_name = "fake"

    async def _fetch_markets(self, session):
        return []

    def _subscribe_frames(self, symbols):
        return [{"op": "subscribe"}]

    def _parse_message(self, message):
        return []

    async def _fetch_funding(self, session, base_asset):
        return None

    def _heartbeat_frame(self):
        return {"method": "PING"}


async def _server(handler):
    app = web.Application()

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await handler(ws)
        return ws

    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"ws://127.0.0.1:{port}/ws"


def _make(url):
    cfg = ScannerConfig()
    cfg.ws_ping_interval_sec = 0.2
    cfg.ws_idle_timeout_sec = 0.6
    a = _Adapter(Settings(), cfg, sink=None, rest_url="http://x", ws_url=url,
                 rate_per_sec=100, burst=100)
    a._shards = [set()]
    a._shard_ws = [None]
    a._shard_tasks = [None]
    a._shard_connected = [False]
    return a, cfg


async def test_heartbeat_sent_and_data_keeps_alive():
    got_ping = asyncio.Event()

    async def handler(ws):
        # Send a data frame, then read client heartbeats.
        await ws.send_str('{"tick": 1}')
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT and "PING" in msg.data:
                got_ping.set()

    runner, url = await _server(handler)
    a, _ = _make(url)
    a._session = aiohttp.ClientSession()
    try:
        task = asyncio.create_task(a._run_shard(0))
        await asyncio.wait_for(got_ping.wait(), timeout=3.0)
        assert a._shard_connected[0] is True
    finally:
        a._stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await a._session.close()
        await runner.cleanup()


async def test_idle_server_triggers_reconnect():
    async def handler(ws):
        # Never send anything; ignore client frames -> connection goes idle.
        async for _ in ws:
            pass

    runner, url = await _server(handler)
    a, cfg = _make(url)
    a._session = aiohttp.ClientSession()
    try:
        # _run_ws_once must raise (idle timeout) rather than hang forever.
        with pytest.raises(Exception) as exc:
            await asyncio.wait_for(a._run_shard(0), timeout=3.0)
        assert "idle" in str(exc.value).lower() or "closed" in str(exc.value).lower()
    finally:
        a._stop.set()
        await a._session.close()
        await runner.cleanup()
