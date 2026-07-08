"""Reconciliation Scheduler (Scanner §1.5 / §16) — the safety net, NOT primary detection.

Fires at most every 1s: re-runs all detectors across the full pair×venue matrix to catch
anything the event path missed (dropped WS message, REST-only data, silent gap), sweeps
age-based expiries (§12.4), and runs passive staleness health checks (§2.2). Always scans
all three priority tiers (§1.6).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from src.config import describe_exc, get_logger
from src.config.scanner_config import ScannerConfig

log = get_logger("scanner.reconciliation")


class ReconciliationScheduler:
    def __init__(
        self, config: ScannerConfig,
        run_full_scan: Callable[[], Awaitable[None]],
        sweep_expired: Callable[[], Awaitable[int]],
        health_pass: Callable[[], Awaitable[None]],
    ) -> None:
        self._config = config
        self._run_full_scan = run_full_scan
        self._sweep_expired = sweep_expired
        self._health_pass = health_pass
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="reconciliation")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            interval = self._config.reconciliation_interval_sec
            try:
                await self._sweep_expired()
                await self._health_pass()
                await self._run_full_scan()
            except Exception as exc:  # noqa: BLE001 — safety net must never die
                log.warning("reconciliation_error", error=describe_exc(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass
