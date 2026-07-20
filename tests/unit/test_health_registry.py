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


# ── P1.4: two-stage DEX staleness (Degraded → Maintenance) ──
def _dex_reg() -> HealthRegistry:
    r = HealthRegistry(ScannerConfig())
    r.register("uniswap_ethereum")
    return r


def test_dex_stale_goes_degraded_not_maintenance():
    """A stale-but-not-dead DEX venue goes DEGRADED (kept in the active pool, signals still
    allowed) rather than straight to Maintenance — one slow public-RPC cycle no longer
    drops the venue."""
    r = _dex_reg()
    r.record_success("uniswap_ethereum", stream=True)
    quiet = r.health("uniswap_ethereum").last_ws_data_at + 90  # past 60s, under 240s
    r.check_staleness("uniswap_ethereum", threshold_sec=60.0, now=quiet,
                      offline_after_sec=240.0)
    assert r.status("uniswap_ethereum") == ExchangeStatus.DEGRADED
    assert r.is_online("uniswap_ethereum")  # still signal-eligible


def test_dex_sustained_staleness_escalates_to_maintenance():
    r = _dex_reg()
    r.record_success("uniswap_ethereum", stream=True)
    base = r.health("uniswap_ethereum").last_ws_data_at
    r.check_staleness("uniswap_ethereum", threshold_sec=60.0, now=base + 90,
                      offline_after_sec=240.0)
    assert r.status("uniswap_ethereum") == ExchangeStatus.DEGRADED
    # Still no fresh data well past the offline window → escalate.
    r.check_staleness("uniswap_ethereum", threshold_sec=60.0, now=base + 300,
                      offline_after_sec=240.0)
    assert r.status("uniswap_ethereum") == ExchangeStatus.MAINTENANCE
    assert not r.is_online("uniswap_ethereum")


def test_degraded_recovers_online_on_first_fresh_tick():
    """DEGRADED never left the active pool, so a single fresh pool read restores ONLINE
    immediately — no N-consecutive anti-flap gate (that only guards true offline)."""
    r = _dex_reg()
    r.record_success("uniswap_ethereum", stream=True)
    quiet = r.health("uniswap_ethereum").last_ws_data_at + 90
    r.check_staleness("uniswap_ethereum", threshold_sec=60.0, now=quiet,
                      offline_after_sec=240.0)
    assert r.status("uniswap_ethereum") == ExchangeStatus.DEGRADED
    r.record_success("uniswap_ethereum", stream=True)  # one fresh tick
    assert r.status("uniswap_ethereum") == ExchangeStatus.ONLINE


def test_degraded_health_factor_is_reduced_but_nonzero():
    r = _dex_reg()
    r.record_success("uniswap_ethereum", stream=True, latency_ms=10.0)
    online_factor = r.health_factor("uniswap_ethereum")
    quiet = r.health("uniswap_ethereum").last_ws_data_at + 90
    r.check_staleness("uniswap_ethereum", threshold_sec=60.0, now=quiet,
                      offline_after_sec=240.0)
    degraded_factor = r.health_factor("uniswap_ethereum")
    assert 0 < degraded_factor < online_factor


def test_cex_staleness_still_single_stage_maintenance():
    """CEX behaviour is unchanged: no offline_after_sec → straight to Maintenance, never
    DEGRADED (the 'don't touch CEX feeds' constraint)."""
    r = _reg()
    r.record_success("mexc", stream=True)
    quiet = r.health("mexc").last_ws_data_at + 999
    r.check_staleness("mexc", threshold_sec=15.0, now=quiet)
    assert r.status("mexc") == ExchangeStatus.MAINTENANCE


def test_streamed_venue_does_not_recover_on_probe_only_success():
    """Production DEX loop: a venue that HAS streamed (a DEX pool feed) must not bounce
    back to Online on bare RPC-probe successes (eth_blockNumber) while its pool reads
    stay dead — that recreated Online → Maintenance → API Offline → Online churn.
    Recovery requires a real market-data (stream=True) tick."""
    r = HealthRegistry(ScannerConfig())
    r.register("uniswap_ethereum")
    r.record_success("uniswap_ethereum", stream=True)     # venue has a live feed
    assert r.status("uniswap_ethereum") == ExchangeStatus.ONLINE
    r.mark_maintenance("uniswap_ethereum")                # stream went stale
    # Many probe-only (eth_blockNumber) successes: RPC is up, but no pool data.
    for _ in range(10):
        r.record_success("uniswap_ethereum", stream=False)
        assert r.status("uniswap_ethereum") == ExchangeStatus.MAINTENANCE
    # Real pool reads resume → recovers (consecutive_success already high → first tick).
    r.record_success("uniswap_ethereum", stream=True)
    assert r.status("uniswap_ethereum") == ExchangeStatus.ONLINE


def test_streamed_venue_recovers_on_sustained_real_data():
    """Sanity: recovery from API Offline still works, driven purely by stream ticks."""
    r = HealthRegistry(ScannerConfig())
    r.register("uniswap_ethereum")
    r.record_success("uniswap_ethereum", stream=True)
    r.mark_offline("uniswap_ethereum")
    cfg = ScannerConfig()
    for _ in range(cfg.health_recovery_consecutive - 1):
        r.record_success("uniswap_ethereum", stream=True)
        assert r.status("uniswap_ethereum") == ExchangeStatus.API_OFFLINE
    r.record_success("uniswap_ethereum", stream=True)
    assert r.status("uniswap_ethereum") == ExchangeStatus.ONLINE
