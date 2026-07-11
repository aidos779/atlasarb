"""Sustained whole-network RPC outage → escalated `rpc_network_down` alert (audit item 2).

The engine already logs per-call `rpc_all_providers_failed`; this adds a distinct,
duration-gated `rpc_network_down` event (and a `rpc_network_recovered` clear) so external
alerting can react to a network that has been fully dark for longer than the threshold.
The two methods are exercised in isolation against a real RpcProviderPool.
"""
from __future__ import annotations

from types import SimpleNamespace

from structlog.testing import capture_logs

from src.config.scanner_config import ScannerConfig
from src.scanner.adapters.rpc_pool import RpcErrorKind, RpcProviderPool
from src.scanner.engine import ScanningEngine


def _engine_stub(pool: RpcProviderPool, network: str, cfg: ScannerConfig):
    """Minimal stand-in exposing exactly what the two outage methods read."""
    adapter = SimpleNamespace(_rpc_pool=pool, network=network)
    stub = SimpleNamespace(_adapters={"dex": adapter}, _config=cfg, _rpc_down_since={})
    stub._rpc_healthy_by_network = lambda: ScanningEngine._rpc_healthy_by_network(stub)
    return stub


def test_sustained_outage_emits_alert_then_recovery():
    cfg = ScannerConfig(rpc_network_down_alert_sec=60.0)
    pool = RpcProviderPool(urls=["u1", "u2"], fail_threshold=1, network="bnb")
    stub = _engine_stub(pool, "bnb", cfg)

    # Both providers down → network fully dark.
    pool.record_failure("u1", RpcErrorKind.TIMEOUT)
    pool.record_failure("u2", RpcErrorKind.TIMEOUT)
    assert pool.healthy_count() == (0, 2)

    # First observation: tracked, but below the threshold → no alert yet.
    with capture_logs() as logs:
        ScanningEngine._check_rpc_network_outages(stub, now=1000.0)
    assert stub._rpc_down_since["bnb"] == 1000.0
    assert not [e for e in logs if e["event"] == "rpc_network_down"]

    # Past the threshold → escalated alert with the elapsed duration.
    with capture_logs() as logs:
        ScanningEngine._check_rpc_network_outages(stub, now=1061.0)
    down = [e for e in logs if e["event"] == "rpc_network_down"]
    assert len(down) == 1
    assert down[0]["network"] == "bnb" and down[0]["down_for_sec"] == 61.0

    # A single recovered provider clears the outage and emits the recovery event.
    pool.record_success("u1", latency_ms=50)
    assert pool.healthy_count() == (1, 2)
    with capture_logs() as logs:
        ScanningEngine._check_rpc_network_outages(stub, now=1200.0)
    assert "bnb" not in stub._rpc_down_since
    assert [e for e in logs if e["event"] == "rpc_network_recovered"]


def test_healthy_network_never_alerts():
    cfg = ScannerConfig(rpc_network_down_alert_sec=60.0)
    pool = RpcProviderPool(urls=["u1", "u2"], fail_threshold=1, network="ethereum")
    stub = _engine_stub(pool, "ethereum", cfg)
    with capture_logs() as logs:
        ScanningEngine._check_rpc_network_outages(stub, now=5000.0)
    assert stub._rpc_down_since == {}
    assert not [e for e in logs if e["event"] == "rpc_network_down"]
