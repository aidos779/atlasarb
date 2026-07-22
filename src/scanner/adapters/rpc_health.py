"""Startup RPC endpoint health probe (diagnostics only — never on the hot path).

Fires a single ``eth_blockNumber`` at every configured RPC endpoint for the EVM DEX
networks with a short timeout and logs which are alive, their latency, and a per-network
live/total summary. Purely observational: it does not gate the pool (the health-scored
RpcProviderPool still decides rotation at runtime) — it just makes "how many nodes were
actually reachable at boot" visible, which is the first thing to check if signal delivery
is still slow after the pool was widened. Run as a fire-and-forget task at startup so it
never delays or blocks the scanner/notifier.
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

from src.config import describe_exc, get_logger, redact_url
from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.scanner.adapters.tls import ssl_context

log = get_logger("adapter.rpc.health")

# The two heaviest DEX networks that went dark on public nodes; also the only EVM chains
# whose RPC latency was implicated in delivery lag. Others use the same failover and can
# be added here if needed.
_EVM_DEX_NETWORKS = ("ethereum", "bnb")
_BLOCK_PROBE = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}


async def _probe_one(session: aiohttp.ClientSession, url: str) -> dict:
    started = time.perf_counter()
    try:
        async with session.post(url, json=_BLOCK_PROBE) as resp:
            alive = False
            if resp.status == 200:
                data = await resp.json(content_type=None)
                alive = isinstance(data, dict) and data.get("result") is not None
            return {"url": url, "alive": alive, "status": resp.status,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except (TimeoutError, aiohttp.ServerTimeoutError):
        return {"url": url, "alive": False, "status": "timeout",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001 — connection reset / DNS / TLS
        return {"url": url, "alive": False, "status": describe_exc(exc),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1)}


async def probe_networks(
    settings: Settings, config: ScannerConfig,
    networks: tuple[str, ...] = _EVM_DEX_NETWORKS,
) -> dict[str, list[dict]]:
    """Probe every configured endpoint of each network concurrently. Returns
    {network: [per-endpoint result, ...]}. Never raises for a dead endpoint."""
    timeout = aiohttp.ClientTimeout(total=config.healthcheck_timeout_sec)
    connector = aiohttp.TCPConnector(ssl=ssl_context())
    results: dict[str, list[dict]] = {}
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for net in networks:
            urls = settings.rpc_urls_for(net)
            if not urls:
                continue
            results[net] = list(
                await asyncio.gather(*(_probe_one(session, u) for u in urls)))
    return results


async def log_startup_rpc_health(settings: Settings, config: ScannerConfig) -> None:
    """Probe and log endpoint health once. Safe to launch fire-and-forget."""
    try:
        results = await probe_networks(settings, config)
    except Exception as exc:  # noqa: BLE001 — diagnostics must never crash startup
        log.warning("rpc_startup_healthcheck_failed", error=describe_exc(exc))
        return
    for net, endpoints in results.items():
        for r in sorted(endpoints, key=lambda e: (not e["alive"], e["latency_ms"])):
            emit = log.info if r["alive"] else log.warning
            emit("rpc_endpoint_health", network=net, alive=r["alive"],
                 status=r["status"], latency_ms=r["latency_ms"], url=redact_url(r["url"]))
        live = sum(1 for r in endpoints if r["alive"])
        log.info("rpc_startup_health_summary", network=net,
                 live=live, total=len(endpoints))
