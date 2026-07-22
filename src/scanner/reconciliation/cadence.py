"""Detector reconciliation cadence (Phase 4).

Decides which detectors the reconciliation safety net re-scans on a given pass. Two
levers, both purely about the SAFETY NET — the event path is authoritative and untouched:

  * per-detector base cadence — funding fastest, cross-chain slowest (§1.5/§16);
  * yield-aware backoff — a detector that has published nothing for ``idle_after_sec`` is
    reconciled less often (cadence × ``idle_backoff``), recovering to base cadence the
    instant it publishes again. Detectors are never disabled, only slowed, so eventual
    consistency holds with a bounded max latency of ``base_cadence × idle_backoff``.

Stateless of the cache/detectors: it only tracks timestamps, so it is trivially testable.
"""
from __future__ import annotations

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType


class DetectorReconciliationCadence:
    def __init__(self, config: ScannerConfig, start_now: float) -> None:
        self._base: dict[str, float] = {}
        self._idle_after = 0.0
        self._idle_backoff = 1.0
        self.update_config(config)
        # Force every detector due on the first pass; treat startup as "active" so cold
        # detectors run at base cadence for the first idle window rather than pre-backed-off.
        self._last_reconciled: dict[str, float] = {a: float("-inf") for a in self._base}
        self._last_publish: dict[str, float] = {a: start_now for a in self._base}

    def update_config(self, config: ScannerConfig) -> None:
        self._base = {
            ArbitrageType.FUNDING.value: config.reconciliation_cadence_funding_sec,
            ArbitrageType.CEX_CEX.value: config.reconciliation_cadence_cex_cex_sec,
            ArbitrageType.CEX_DEX.value: config.reconciliation_cadence_cex_dex_sec,
            ArbitrageType.DEX_DEX.value: config.reconciliation_cadence_dex_dex_sec,
            ArbitrageType.CROSS_CHAIN.value: config.reconciliation_cadence_cross_chain_sec,
        }
        self._idle_after = config.reconciliation_idle_after_sec
        self._idle_backoff = max(1.0, config.reconciliation_idle_backoff)

    def effective_cadence(self, arb_type: str, now: float) -> float:
        """Base cadence, ×idle_backoff when the detector has been cold past idle_after."""
        base = self._base.get(arb_type, 0.0)
        last_pub = self._last_publish.get(arb_type, now)
        if self._idle_after > 0 and (now - last_pub) >= self._idle_after:
            return base * self._idle_backoff
        return base

    def due_detectors(self, now: float) -> set[str]:
        """arb types whose reconciliation cadence has elapsed this pass (cadence 0 = every
        pass). Membership here is a *candidate* to run — per-pair eligibility narrows it."""
        due: set[str] = set()
        for arb, base in self._base.items():
            if base <= 0:
                due.add(arb)
                continue
            elapsed = now - self._last_reconciled.get(arb, float("-inf"))
            if elapsed >= self.effective_cadence(arb, now):
                due.add(arb)
        return due

    def mark_reconciled(self, arb_types: set[str], now: float) -> None:
        for arb in arb_types:
            self._last_reconciled[arb] = now

    def mark_publish(self, arb_type: str, now: float) -> None:
        """Reset the yield timer so a detector recovers to base cadence on any publish."""
        if arb_type in self._last_publish:
            self._last_publish[arb_type] = now

    def cadence_report(self, now: float) -> dict[str, float]:
        return {arb: round(self.effective_cadence(arb, now), 2) for arb in self._base}
