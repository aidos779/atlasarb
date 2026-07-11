"""Startup RPC health probe: classifies alive/dead/timeout per endpoint and logs a
per-network live/total summary, without ever raising for a dead endpoint.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from structlog.testing import capture_logs

from src.config.scanner_config import ScannerConfig
from src.scanner.adapters import rpc_health


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {"result": "0x1"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    def __init__(self, handlers):
        self._handlers = handlers

    def post(self, url, json=None):
        return self._handlers[url]()


def test_probe_one_classifies_alive_dead_timeout():
    handlers = {
        "ok": lambda: _Resp(payload={"result": "0x10"}),
        "null": lambda: _Resp(payload={"result": None}),   # degraded node
        "http500": lambda: _Resp(status=500),
    }
    session = _FakeSession(handlers)

    async def run():
        return {u: await rpc_health._probe_one(session, u) for u in handlers}

    res = asyncio.run(run())
    assert res["ok"]["alive"] is True
    assert res["null"]["alive"] is False
    assert res["http500"]["alive"] is False and res["http500"]["status"] == 500


def test_log_startup_summary_counts_live(monkeypatch):
    async def fake_probe(settings, config, networks=rpc_health._EVM_DEX_NETWORKS):
        return {
            "ethereum": [
                {"url": "a", "alive": True, "status": 200, "latency_ms": 30.0},
                {"url": "b", "alive": False, "status": "timeout", "latency_ms": 3000.0},
                {"url": "c", "alive": True, "status": 200, "latency_ms": 90.0},
            ],
        }

    monkeypatch.setattr(rpc_health, "probe_networks", fake_probe)
    with capture_logs() as logs:
        asyncio.run(rpc_health.log_startup_rpc_health(SimpleNamespace(), ScannerConfig()))

    summary = [e for e in logs if e["event"] == "rpc_startup_health_summary"]
    assert len(summary) == 1
    assert summary[0]["network"] == "ethereum"
    assert summary[0]["live"] == 2 and summary[0]["total"] == 3
