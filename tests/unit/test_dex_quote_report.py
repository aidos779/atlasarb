"""Per-DEX-venue quote-freshness diagnostic (audit item 4).

Distinguishes "no fresh DEX quotes (RPC starvation)" from "quotes fresh, no arb": a stale
venue is logged at WARNING, a fresh one at DEBUG, one event per DEX venue.
"""
from __future__ import annotations

from types import SimpleNamespace

from structlog.testing import capture_logs

from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.scanner.engine import ScanningEngine
from src.scanner.status.health_registry import HealthRegistry


def _stub(health: HealthRegistry, adapters: dict, cfg: ScannerConfig):
    stub = SimpleNamespace(_adapters=adapters, _health=health, _config=cfg)
    return stub


def test_dex_quote_report_flags_stale_vs_fresh():
    cfg = ScannerConfig()
    health = HealthRegistry(cfg)
    fresh = SimpleNamespace(venue_type=VenueType.DEX)
    stale = SimpleNamespace(venue_type=VenueType.DEX)
    cex = SimpleNamespace(venue_type=VenueType.CEX)
    health.register("uni")
    health.register("sushi")
    health.register("binance")
    for _ in range(3):
        health.record_success("uni", stream=True)   # fresh quotes
    # "sushi" never received a quote → stale.
    stub = _stub(health, {"uni": fresh, "sushi": stale, "binance": cex}, cfg)

    with capture_logs() as logs:
        ScanningEngine._log_dex_quote_report(stub)

    events = {e["venue"]: e for e in logs if e["event"] == "dex_quote_status"}
    # One event per DEX venue only (CEX excluded).
    assert set(events) == {"uni", "sushi"}
    assert events["uni"]["quote_fresh"] is True and events["uni"]["log_level"] == "debug"
    assert events["sushi"]["quote_fresh"] is False and events["sushi"]["log_level"] == "warning"
