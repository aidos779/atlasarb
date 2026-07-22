"""Per-detector diagnostics (§17): counter aggregation, attribution and periodic reset."""
from __future__ import annotations

from src.bot.formatters.stats import format_detector_stats
from src.domain.enums import ArbitrageType
from src.scanner.detectors.base import Detector
from src.scanner.monitoring.detector_stats import REPORT_KEYS, DetectorStats


class _FakeDetector(Detector):
    def __init__(self, arb_type: str) -> None:
        super().__init__()
        self.arb_type = arb_type

    def detect(self, ctx, base_asset, quote_asset):  # pragma: no cover — unused
        return []


def _stats() -> DetectorStats:
    return DetectorStats([_FakeDetector(a.value) for a in ArbitrageType])


def test_report_covers_every_detector() -> None:
    report = _stats().report_and_reset()
    assert set(report) == set(REPORT_KEYS)
    assert set(report) == {"cex_cex", "cex_dex", "dex_dex", "funding", "cross_chain"}


def test_empty_report_is_all_zero() -> None:
    assert _stats().report_and_reset()["funding"] == {
        "checked": 0, "candidates": 0, "published": 0, "rejected_spread": 0,
        "rejected_fees": 0, "rejected_liquidity": 0, "rejected_validation": 0,
        "rejected_economic": 0, "rejected_bridge": 0,
        "asm_rejected_spread": 0, "asm_rejected_fees": 0,
        "asm_rejected_liquidity": 0, "asm_rejected_validation": 0,
    }


def test_detector_owned_counters_reach_the_report() -> None:
    detector = _FakeDetector(ArbitrageType.CEX_CEX.value)
    stats = DetectorStats([detector])
    detector.counters.opportunities_checked += 3
    detector.counters.rejected_by_spread += 2

    report = stats.report_and_reset()

    assert report["cex_cex"]["checked"] == 3
    assert report["cex_cex"]["rejected_spread"] == 2


def test_candidates_and_publishes_are_attributed_by_arb_type() -> None:
    stats = _stats()
    stats.record_candidate(ArbitrageType.DEX_DEX.value)
    stats.record_candidate(ArbitrageType.DEX_DEX.value)
    stats.record_candidate(ArbitrageType.FUNDING.value)
    stats.record_published(ArbitrageType.DEX_DEX.value)

    report = stats.report_and_reset()

    assert report["dex_dex"]["candidates"] == 2
    assert report["dex_dex"]["published"] == 1
    assert report["funding"]["candidates"] == 1
    assert report["funding"]["published"] == 0
    assert report["cex_cex"]["candidates"] == 0


def test_reject_reasons_map_to_the_assembler_buckets() -> None:
    # record_rejection attributes ASSEMBLER-side rejects (a candidate the detector emitted
    # that the downstream pipeline dropped) to the asm_rejected_* columns — kept separate
    # from the detector's own pre-candidate drops so the two never double-count.
    stats = _stats()
    cex_cex = ArbitrageType.CEX_CEX.value
    for reason in ("BELOW_MIN_PROFIT", "IMPLAUSIBLE_SPREAD"):
        stats.record_rejection(cex_cex, reason)
    for reason in ("UNPROFITABLE_AFTER_FEES", "MISSING_FEE_DATA", "GAS_EXCEEDS_LIMIT"):
        stats.record_rejection(cex_cex, reason)
    stats.record_rejection(cex_cex, "INSUFFICIENT_LIQUIDITY")
    # Freshness and risk both collapse into "validation".
    stats.record_rejection(cex_cex, "STALE_DATA")
    stats.record_rejection(cex_cex, "RISK_TOO_HIGH")

    counters = stats.report_and_reset()["cex_cex"]

    assert counters["asm_rejected_spread"] == 2
    assert counters["asm_rejected_fees"] == 3
    assert counters["asm_rejected_liquidity"] == 1
    assert counters["asm_rejected_validation"] == 2
    # Detector-side columns are untouched by assembler attribution.
    assert counters["rejected_spread"] == 0
    assert counters["rejected_fees"] == 0
    assert counters["rejected_validation"] == 0


def test_unknown_reject_reason_counts_as_validation() -> None:
    stats = _stats()
    stats.record_rejection(ArbitrageType.CEX_DEX.value, "SOME_FUTURE_REASON")
    assert stats.report_and_reset()["cex_dex"]["asm_rejected_validation"] == 1


def test_unknown_arb_type_is_ignored_not_raised() -> None:
    stats = _stats()
    stats.record_candidate("NOT_A_TYPE")
    stats.record_rejection("NOT_A_TYPE", "STALE_DATA")
    stats.record_published("NOT_A_TYPE")
    assert all(sum(c.values()) == 0 for c in stats.report_and_reset().values())


# ── periodic reset ──
def test_counters_reset_after_each_report() -> None:
    detector = _FakeDetector(ArbitrageType.CEX_CEX.value)
    stats = DetectorStats([detector])
    detector.counters.opportunities_checked += 10
    stats.record_candidate(ArbitrageType.CEX_CEX.value)
    stats.record_published(ArbitrageType.CEX_CEX.value)

    first = stats.report_and_reset()
    second = stats.report_and_reset()

    assert first["cex_cex"]["checked"] == 10
    assert first["cex_cex"]["candidates"] == 1
    assert second["cex_cex"] == {
        "checked": 0, "candidates": 0, "published": 0, "rejected_spread": 0,
        "rejected_fees": 0, "rejected_liquidity": 0, "rejected_validation": 0,
        "rejected_economic": 0, "rejected_bridge": 0,
        "asm_rejected_spread": 0, "asm_rejected_fees": 0,
        "asm_rejected_liquidity": 0, "asm_rejected_validation": 0,
    }


def test_each_interval_reports_only_its_own_activity() -> None:
    detector = _FakeDetector(ArbitrageType.FUNDING.value)
    stats = DetectorStats([detector])

    detector.counters.opportunities_checked += 5
    stats.report_and_reset()
    detector.counters.opportunities_checked += 2
    second = stats.report_and_reset()

    assert second["funding"]["checked"] == 2  # not 7 — the window is per-interval


def test_report_snapshot_is_not_aliased_to_live_counters() -> None:
    detector = _FakeDetector(ArbitrageType.CEX_CEX.value)
    stats = DetectorStats([detector])
    detector.counters.opportunities_checked += 4

    report = stats.report_and_reset()
    detector.counters.opportunities_checked += 99

    assert report["cex_cex"]["checked"] == 4


# ── last_report / live_report ──
def test_last_report_is_all_zero_before_the_first_interval() -> None:
    stats = _stats()
    assert set(stats.last_report) == set(REPORT_KEYS)
    assert all(sum(c.values()) == 0 for c in stats.last_report.values())


def test_last_report_holds_the_completed_interval_not_the_current_one() -> None:
    detector = _FakeDetector(ArbitrageType.CEX_CEX.value)
    stats = DetectorStats([detector])

    detector.counters.opportunities_checked += 7
    stats.report_and_reset()
    detector.counters.opportunities_checked += 3  # partial, current interval

    assert stats.last_report["cex_cex"]["checked"] == 7
    assert stats.live_report()["cex_cex"]["checked"] == 3


# ── /stats rendering ──
def test_stats_screen_lists_every_detector_and_sums_rejections() -> None:
    stats = _stats()
    cex_cex = ArbitrageType.CEX_CEX.value
    stats.record_candidate(cex_cex)
    stats.record_published(cex_cex)
    stats.record_rejection(cex_cex, "BELOW_MIN_PROFIT")
    stats.record_rejection(cex_cex, "INSUFFICIENT_LIQUIDITY")

    text = format_detector_stats(stats.report_and_reset())

    for heading in ("CEX↔CEX", "CEX↔DEX", "DEX↔DEX", "Funding", "Cross-chain"):
        assert heading in text
    assert "Engine Statistics (last minute)" in text
    # record_rejection is assembler-side, so it lands under Assembler-rejected, not the
    # detector-dropped column (which stays 0 here).
    assert "Assembler-rejected: 2" in text  # 1 spread + 1 liquidity
    assert "Detector-dropped: 0" in text
    assert "- spread 1" in text
    assert "- liquidity 1" in text
