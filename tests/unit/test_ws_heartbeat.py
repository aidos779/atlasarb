"""Per-venue WS application-level heartbeat frames (§2.3).

Regression for the MEXC/OKX TimeoutError churn: each venue must send its own
keepalive payload rather than relying on aiohttp control-frame heartbeat.
"""
import pytest

from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.scanner.adapters.cex.binance import BinanceAdapter
from src.scanner.adapters.cex.bitget import BitgetAdapter
from src.scanner.adapters.cex.mexc import MexcAdapter
from src.scanner.adapters.cex.okx import OkxAdapter

pytestmark = pytest.mark.asyncio


def _a(cls):
    return cls(Settings(), ScannerConfig(), sink=None)


async def test_heartbeat_frames_match_venue_protocol():
    assert _a(MexcAdapter)._heartbeat_frame() == {"method": "PING"}
    assert _a(OkxAdapter)._heartbeat_frame() == "ping"
    assert _a(BitgetAdapter)._heartbeat_frame() == "ping"
    # Binance server pings us; aiohttp autoping answers -> no app frame needed.
    assert _a(BinanceAdapter)._heartbeat_frame() is None


class _FakeWs:
    def __init__(self):
        self.sent_json = []
        self.sent_str = []

    async def send_json(self, x):
        self.sent_json.append(x)

    async def send_str(self, x):
        self.sent_str.append(x)


async def test_send_heartbeat_dispatches_by_type():
    ws = _FakeWs()
    await _a(MexcAdapter)._send_heartbeat(ws)
    assert ws.sent_json == [{"method": "PING"}]

    ws2 = _FakeWs()
    await _a(OkxAdapter)._send_heartbeat(ws2)
    assert ws2.sent_str == ["ping"]

    ws3 = _FakeWs()
    await _a(BinanceAdapter)._send_heartbeat(ws3)  # None -> no-op
    assert ws3.sent_json == [] and ws3.sent_str == []
