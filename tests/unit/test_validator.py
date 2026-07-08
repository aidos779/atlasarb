from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ExchangeStatus, RejectReason
from src.domain.signal import ProfitBreakdown, SizingProfile
from src.scanner.validation.validator import (
    SignalValidator,
    ValidationContext,
)

_ALL_FEES = frozenset({"trading", "withdrawal", "gas", "bridge", "slippage"})


def _breakdown(net_usd=Decimal("50"), roi=Decimal("2"), gross_pct=Decimal("1.5")):
    return ProfitBreakdown(
        size_usd=Decimal("1000"), gross_profit_usd=Decimal("60"),
        trading_fees_usd=Decimal("5"), withdrawal_fees_usd=Decimal("1"),
        gas_fees_usd=Decimal("2"), bridge_fees_usd=Decimal("0"),
        slippage_cost_usd=Decimal("2"), conversion_cost_usd=Decimal("0"),
        net_profit_usd=net_usd, roi_pct=roi, gross_spread_pct=gross_pct,
        net_profit_pct=roi, resolved_categories=_ALL_FEES,
    )


def _ctx(**overrides):
    base = dict(
        breakdown=_breakdown(),
        sizing=SizingProfile(Decimal("1000"), Decimal("5000"), Decimal("3000"),
                             Decimal("5000"), []),
        liquidity_usd=Decimal("20000"), liquidity_score=Decimal("80"),
        confidence_score=90, buy_status=ExchangeStatus.ONLINE,
        sell_status=ExchangeStatus.ONLINE, max_data_staleness_sec=1.0,
        max_allowed_staleness_sec=15.0, venue_type_for_floor="CEX",
        token_verified=True, warmed_up=True, gas_fee_usd=Decimal("2"),
        gross_profit_usd=Decimal("60"),
    )
    base.update(overrides)
    return ValidationContext(**base)


def test_valid_candidate_passes():
    v = SignalValidator(ScannerConfig())
    assert v.validate(_ctx()).ok


def test_below_min_profit_rejected():
    v = SignalValidator(ScannerConfig())
    result = v.validate(_ctx(breakdown=_breakdown(net_usd=Decimal("0.01"),
                                                  roi=Decimal("0.001"))))
    assert result.reason == RejectReason.BELOW_MIN_PROFIT


def test_unverified_token_rejected():
    v = SignalValidator(ScannerConfig())
    assert v.validate(_ctx(token_verified=False)).reason == RejectReason.UNVERIFIED_TOKEN


def test_offline_exchange_rejected():
    v = SignalValidator(ScannerConfig())
    r = v.validate(_ctx(sell_status=ExchangeStatus.API_OFFLINE))
    assert r.reason == RejectReason.EXCHANGE_NOT_ONLINE


def test_low_confidence_rejected():
    v = SignalValidator(ScannerConfig())
    assert v.validate(_ctx(confidence_score=10)).reason == RejectReason.LOW_CONFIDENCE


def test_stale_data_rejected():
    v = SignalValidator(ScannerConfig())
    assert v.validate(_ctx(max_data_staleness_sec=999)).reason == RejectReason.STALE_DATA


def test_not_warmed_up_rejected():
    v = SignalValidator(ScannerConfig())
    assert v.validate(_ctx(warmed_up=False)).reason == RejectReason.NOT_WARMED_UP


def test_gas_exceeds_limit_rejected():
    v = SignalValidator(ScannerConfig())
    r = v.validate(_ctx(gas_fee_usd=Decimal("40"), gross_profit_usd=Decimal("60")))
    assert r.reason == RejectReason.GAS_EXCEEDS_LIMIT


def test_missing_fee_data_rejected():
    v = SignalValidator(ScannerConfig())
    bd = _breakdown()
    bd.resolved_categories = frozenset({"trading", "slippage"})
    assert v.validate(_ctx(breakdown=bd)).reason == RejectReason.MISSING_FEE_DATA
