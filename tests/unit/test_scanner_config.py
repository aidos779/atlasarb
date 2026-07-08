import pytest

from src.config.scanner_config import (
    ConfigError,
    ConfigManager,
    ScannerConfig,
    validate_config,
)


def test_valid_default_config():
    validate_config(ScannerConfig())  # should not raise


def test_cooldown_cannot_exceed_ttl():
    cfg = ScannerConfig()
    cfg.cooldown_by_type["CEX_CEX"] = 999999
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_dex_slippage_below_cex_rejected():
    cfg = ScannerConfig()
    cfg.max_slippage_dex_pct = 0.5
    cfg.max_slippage_cex_pct = 1.0
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_negative_min_profit_rejected():
    cfg = ScannerConfig()
    cfg.min_net_profit_usd = -1
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_hot_reload_and_layering():
    mgr = ConfigManager(ScannerConfig())
    assert mgr.introspect("min_net_profit_usd")[1] == "base"
    mgr.apply_environment_layer({"min_net_profit_usd": 2.0})
    assert mgr.config.min_net_profit_usd == 2.0
    assert mgr.introspect("min_net_profit_usd")[1] == "environment"
    mgr.apply_runtime_override({"min_net_profit_usd": 3.0})
    assert mgr.config.min_net_profit_usd == 3.0
    assert mgr.introspect("min_net_profit_usd")[1] == "runtime"


def test_invalid_runtime_override_is_atomic():
    mgr = ConfigManager(ScannerConfig())
    before = mgr.config.max_slippage_dex_pct
    with pytest.raises(ConfigError):
        mgr.apply_runtime_override({"max_slippage_dex_pct": 0.01})
    assert mgr.config.max_slippage_dex_pct == before  # unchanged


def test_subscriber_notified_on_reload():
    mgr = ConfigManager(ScannerConfig())
    seen = []
    mgr.subscribe(lambda cfg: seen.append(cfg.min_net_profit_usd))
    mgr.apply_runtime_override({"min_net_profit_usd": 7.0})
    assert seen == [7.0]
