from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, RankingLevel, RiskScore
from src.scanner.ranking.ranker import (
    RankingEngine,
    RankInputs,
    RiskInputs,
    classify_risk,
)


def test_risk_low_for_atomic_dex():
    risk = classify_risk(RiskInputs(
        arb_type=ArbitrageType.DEX_DEX, liquidity_usd=Decimal("50000"),
        liquidity_floor=Decimal("10000"), cross_network_transfer=False,
        bridge_time_sec=None, atomic_execution=True, price_source_count=5))
    assert risk == RiskScore.LOW


def test_risk_high_for_cross_chain():
    risk = classify_risk(RiskInputs(
        arb_type=ArbitrageType.CROSS_CHAIN, liquidity_usd=Decimal("15000"),
        liquidity_floor=Decimal("10000"), cross_network_transfer=True,
        bridge_time_sec=600, atomic_execution=False, price_source_count=1))
    assert risk == RiskScore.HIGH


def _rank_inputs(**kw):
    base = dict(
        net_profit_usd=Decimal("400"),
        profit_reference_usd=Decimal("500"), liquidity_score=Decimal("90"),
        confidence_score=Decimal("95"), risk=RiskScore.LOW,
        arb_type=ArbitrageType.CEX_CEX, bridge_time_sec=None, warmed_up=True)
    base.update(kw)
    return RankInputs(**base)


def test_cross_chain_cannot_be_top_without_fast_bridge():
    r = RankingEngine(ScannerConfig())
    inp = _rank_inputs(arb_type=ArbitrageType.CROSS_CHAIN, bridge_time_sec=600)
    tier = r.assign_tier(99.0, inp)
    assert tier != RankingLevel.TOP


def test_cross_chain_top_allowed_with_fast_bridge():
    r = RankingEngine(ScannerConfig())
    inp = _rank_inputs(arb_type=ArbitrageType.CROSS_CHAIN, bridge_time_sec=60)
    assert r.assign_tier(99.0, inp) == RankingLevel.TOP


def test_not_warmed_up_capped_at_medium():
    r = RankingEngine(ScannerConfig())
    assert r.assign_tier(99.0, _rank_inputs(warmed_up=False)) == RankingLevel.MEDIUM
