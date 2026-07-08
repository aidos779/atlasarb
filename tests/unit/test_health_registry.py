"""Health Registry state-machine regression tests (Scanner §14).

Covers the production-log defects: false Maintenance flaps, venues stuck in
UNKNOWN, and the WS-vs-REST staleness confusion.
"""
from src.config.scanner_config import ScannerConfig
from src.domain.enums import ExchangeStatus
from src.scanner.status.health_registry import HealthRegistry


def _reg() -> HealthRegistry:
    r = HealthRegistry(ScannerConfig())
    r.register("mexc")
    return r


def test_cold_start_first_success_goes_online():
    # A venue must not get stuck in UNKNOWN: the first healthy check brings it up.
    r = _reg()
    assert r.status("mexc") == ExchangeStatus.UNKNOWN
    r.record_success("mexc")
    assert r.status("mexc") == ExchangeStatus.ONLINE


def test_alternating_checks_do_not_strand_in_unknown():
    r = _reg()
    r.record_failure("mexc")   # from UNKNOWN, single soft failure stays UNKNOWN
    r.record_success("mexc")   # first success -> ONLINE (no strand)
    assert r.status("mexc") == ExchangeStatus.ONLINE


def test_recovery_from_offline_needs_n_consecutive():
    # Anti-flap: once degraded, recovery needs N consecutive good checks.
    r = _reg()
    cfg = ScannerConfig()
    r.mark_offline("mexc")
    assert r.status("mexc") == ExchangeStatus.API_OFFLINE
    for _ in range(cfg.health_recovery_consecutive - 1):
        r.record_success("mexc")
        assert r.status("mexc") == ExchangeStatus.API_OFFLINE  # still recovering
    r.record_success("mexc")
    assert r.status("mexc") == ExchangeStatus.ONLINE


def test_staleness_ignores_rest_only_venue():
    # Venue kept alive only by REST health-checks (no WS ticks) must not be
    # marked Maintenance just because a serial health-check round was slow.
    r = _reg()
    r.record_success("mexc", latency_ms=5.0)  # REST success, stream=False
    assert r.status("mexc") == ExchangeStatus.ONLINE
    r.check_staleness("mexc", threshold_sec=15.0, now=r.health("mexc").last_success_at + 999)
    assert r.status("mexc") == ExchangeStatus.ONLINE  # NOT flapped to Maintenance


def test_staleness_degrades_quiet_stream():
    # A venue that streamed data and then went silent legitimately degrades.
    r = _reg()
    r.record_success("mexc", stream=True)
    assert r.status("mexc") == ExchangeStatus.ONLINE
    quiet = r.health("mexc").last_ws_data_at + 999
    r.check_staleness("mexc", threshold_sec=15.0, now=quiet)
    assert r.status("mexc") == ExchangeStatus.MAINTENANCE


def test_fresh_stream_not_degraded():
    r = _reg()
    r.record_success("mexc", stream=True)
    fresh = r.health("mexc").last_ws_data_at + 1
    r.check_staleness("mexc", threshold_sec=15.0, now=fresh)
    assert r.status("mexc") == ExchangeStatus.ONLINE
