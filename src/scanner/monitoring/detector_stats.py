"""Per-detector runtime counters (Scanner §17) — production diagnostics.

Answers one question the aggregate ``engine_status``/``engine_stats`` lines cannot:
*is each individual detector actually doing work right now?* A detector that is wired
up but silently starved (no venues online, no verified tokens, no funding data) is
indistinguishable from a healthy one in the aggregate funnel — here it shows up as a
flat ``checked=0``.

Design constraints (the scanner hot loop runs ~1M detect() calls/s):
  * plain Python ints, incremented in place — no locks, no I/O, no allocation;
  * counters live *on the detector instance*, so the hot path is one attribute load
    plus one ``+= 1`` (see ``Detector.__init__``);
  * everything runs on the single asyncio event loop, so read-modify-write of an int
    is already atomic with respect to other engine code — a lock would only add cost.

``report_and_reset()`` is called once per minute by the engine's stats loop: it snapshots
every detector's counters, zeroes them, and caches the result so the admin ``/stats``
command reports the last *complete* minute rather than a partially-filled window.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from src.domain.enums import ArbitrageType
from src.scanner.monitoring.metrics import Metrics

# Report key order — mirrors the detector list and the /stats screen.
REPORT_KEYS: tuple[str, ...] = tuple(a.value.lower() for a in ArbitrageType)

# The engine-wide reject buckets collapse into the four the per-detector report exposes.
# Freshness and risk rejections are both "the candidate failed validation", which is the
# actionable distinction here — the fine-grained reasons stay in ``engine_stats``.
_BUCKET_FIELD = {
    "spread": "rejected_by_spread",
    "fees": "rejected_by_fees",
    "liquidity": "rejected_by_liquidity",
    "freshness": "rejected_by_validation",
    "risk": "rejected_by_validation",
}


@dataclass(slots=True)
class DetectorCounters:
    """One detector's funnel for the current report interval.

    ``opportunities_checked`` counts ``detect()`` invocations — i.e. how many (base,quote)
    opportunities this detector examined, including the ones it dropped for want of
    input data. That is deliberately the "is it running at all" signal.
    """

    opportunities_checked: int = 0
    candidates_created: int = 0
    rejected_by_spread: int = 0
    rejected_by_fees: int = 0
    rejected_by_liquidity: int = 0
    rejected_by_validation: int = 0
    signals_published: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "checked": self.opportunities_checked,
            "candidates": self.candidates_created,
            "published": self.signals_published,
            "rejected_spread": self.rejected_by_spread,
            "rejected_fees": self.rejected_by_fees,
            "rejected_liquidity": self.rejected_by_liquidity,
            "rejected_validation": self.rejected_by_validation,
        }

    def reset(self) -> None:
        self.opportunities_checked = 0
        self.candidates_created = 0
        self.rejected_by_spread = 0
        self.rejected_by_fees = 0
        self.rejected_by_liquidity = 0
        self.rejected_by_validation = 0
        self.signals_published = 0


class DetectorStats:
    """Aggregates the detectors' own counters into one periodic report.

    Detection-side counters (``checked``, ``rejected_by_spread``) are incremented by the
    detectors themselves. Everything downstream of detection — candidates surviving
    dedup, assembler rejections, published signals — is attributed here by the
    *candidate's* arb type, which is what the report is keyed on. That matters for
    DEX↔DEX: it can emit a CROSS_CHAIN candidate, and that candidate's downstream fate
    belongs under ``cross_chain``, next to the bridge route that decided it.
    """

    def __init__(self, detectors: Iterable[object]) -> None:
        self._by_type: dict[str, DetectorCounters] = {
            d.arb_type: d.counters for d in detectors  # type: ignore[attr-defined]
        }
        # Counters for an arb type no detector owns (none today) would otherwise be
        # dropped silently; give every reportable type a home.
        for key in (a.value for a in ArbitrageType):
            self._by_type.setdefault(key, DetectorCounters())
        self._last_report: dict[str, dict[str, int]] = self._empty_report()

    @staticmethod
    def _empty_report() -> dict[str, dict[str, int]]:
        return {key: DetectorCounters().snapshot() for key in REPORT_KEYS}

    # ── downstream attribution (called from the engine) ──
    def record_candidate(self, arb_type: str) -> None:
        counters = self._by_type.get(arb_type)
        if counters is not None:
            counters.candidates_created += 1

    def record_rejection(self, arb_type: str, reason: str) -> None:
        counters = self._by_type.get(arb_type)
        if counters is None:
            return
        field = _BUCKET_FIELD.get(Metrics.reject_bucket(reason), "rejected_by_validation")
        setattr(counters, field, getattr(counters, field) + 1)

    def record_published(self, arb_type: str) -> None:
        counters = self._by_type.get(arb_type)
        if counters is not None:
            counters.signals_published += 1

    # ── reporting ──
    def report_and_reset(self) -> dict[str, dict[str, int]]:
        """Snapshot every detector's interval counters, then zero them.

        The returned mapping is keyed by lower-cased arb type (``cex_cex``, …) and is
        also cached as ``last_report`` for the admin ``/stats`` command.
        """
        report = {
            arb_type.value.lower(): self._by_type[arb_type.value].snapshot()
            for arb_type in ArbitrageType
        }
        for counters in self._by_type.values():
            counters.reset()
        self._last_report = report
        return report

    @property
    def last_report(self) -> dict[str, dict[str, int]]:
        """The most recently completed interval. All-zero until the first report."""
        return self._last_report

    def live_report(self) -> dict[str, dict[str, int]]:
        """Counters accumulated so far in the *current* (incomplete) interval."""
        return {arb_type.value.lower(): self._by_type[arb_type.value].snapshot()
                for arb_type in ArbitrageType}
