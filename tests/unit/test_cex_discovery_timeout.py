"""Market discovery gets its own larger per-request timeout (audit item 8).

Binance's ~700-pair exchangeInfo repeatedly hit the 5s general REST budget → 26
`discovery_failed TimeoutError`. Discovery now passes `cex_discovery_timeout_sec` as a
per-request override; normal quote calls keep the session default.
"""
from __future__ import annotations

import aiohttp

from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.scanner.adapters.cex.binance import BinanceAdapter


class _Resp:
    def __init__(self, body: bytes):
        self.status = 200
        self._body = body
        self.headers = {}

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingSession:
    def __init__(self, body: bytes):
        self._body = body
        self.last_timeout: aiohttp.ClientTimeout | None = None

    def get(self, url, params=None, timeout=None):
        self.last_timeout = timeout
        return _Resp(self._body)


async def test_discovery_uses_configured_discovery_timeout():
    cfg = ScannerConfig(cex_discovery_timeout_sec=22.0)
    adapter = BinanceAdapter(Settings(), cfg, sink=None)
    session = _RecordingSession(b'{"symbols": []}')
    await adapter._fetch_markets(session)
    assert session.last_timeout is not None
    assert session.last_timeout.total == 22.0   # discovery budget, not the 5s REST default


async def test_plain_get_has_no_per_request_timeout_override():
    adapter = BinanceAdapter(Settings(), ScannerConfig(), sink=None)
    session = _RecordingSession(b'{"ok": true}')
    # A normal call (no timeout_sec) leaves the session-level timeout in force.
    await adapter._get_json_logged(session, "https://x/api")
    assert session.last_timeout is None
