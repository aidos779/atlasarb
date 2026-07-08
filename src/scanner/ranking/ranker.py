"""Ranking Engine (Scanner §11) + Risk classification (§10.5).

Composite score (§11.2) with configurable weights, tier thresholds (§11.3) and the
hard overrides that cannot be outscored (cross-chain TOP suppression, warm-up MEDIUM
cap, funding modified score). Ranking is informational only (PRD BR-RANK-2).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, RankingLevel, RiskScore
from src.scanner import mathx


@dataclass
class RiskInputs:
    arb_type: ArbitrageType
    liquidity_usd: Decimal
    liquidity_floor: Decimal
    cross_network_transfer: bool
    bridge_time_sec: int | None
    atomic_execution: bool
    venue_risk_penalty: Decimal = Decimal(0)   # 0..100 per-venue trust
    price_source_count: int = 1


def classify_risk(inp: RiskInputs) -> RiskScore:
    """PRD §10.5 three-level classification from execution-risk factors."""
    penalty = Decimal(0)
    if inp.arb_type == ArbitrageType.CROSS_CHAIN:
        penalty += Decimal(45)
    elif inp.arb_type == ArbitrageType.CEX_DEX:
        penalty += Decimal(20)
    elif inp.arb_type == ArbitrageType.DEX_DEX and not inp.atomic_execution:
        penalty += Decimal(25)
    if inp.cross_network_transfer:
        penalty += Decimal(15)
    if inp.bridge_time_sec and inp.bridge_time_sec > 300:
        penalty += Decimal(15)
    # Thin liquidity relative to floor.
    if inp.liquidity_floor > 0 and inp.liquidity_usd < inp.liquidity_floor * Decimal(2):
        penalty += Decimal(20)
    if inp.price_source_count < 2:
        penalty += Decimal(10)
    penalty += inp.venue_risk_penalty

    if penalty >= Decimal(45):
        return RiskScore.HIGH
    if penalty >= Decimal(20):
        return RiskScore.MEDIUM
    return RiskScore.LOW


def risk_penalty_value(risk: RiskScore) -> Decimal:
    return {RiskScore.LOW: Decimal(10), RiskScore.MEDIUM: Decimal(40),
            RiskScore.HIGH: Decimal(80)}[risk]


@dataclass
class RankInputs:
    net_profit_usd: Decimal
    roi_pct: Decimal
    profit_reference_usd: Decimal
    liquidity_score: Decimal
    confidence_score: Decimal
    risk: RiskScore
    arb_type: ArbitrageType
    bridge_time_sec: int | None
    warmed_up: bool
    funding_projected_profit: Decimal | None = None


class RankingEngine:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def composite_score(self, inp: RankInputs) -> float:
        cfg = self._config
        # Funding uses projected-period profit in place of one-shot net (§11.3).
        profit_basis = (
            inp.funding_projected_profit
            if inp.arb_type == ArbitrageType.FUNDING and inp.funding_projected_profit is not None
            else inp.net_profit_usd
        )
        profit_norm = mathx.normalize_scale(profit_basis, inp.profit_reference_usd)
        risk_term = Decimal(100) - risk_penalty_value(inp.risk)
        score = (
            Decimal(str(cfg.w_profit)) * profit_norm
            + Decimal(str(cfg.w_liquidity)) * inp.liquidity_score
            + Decimal(str(cfg.w_confidence)) * inp.confidence_score
            + Decimal(str(cfg.w_risk)) * risk_term
        )
        return float(max(Decimal(0), min(Decimal(100), score)))

    def assign_tier(self, score: float, inp: RankInputs) -> RankingLevel:
        cfg = self._config
        if score >= cfg.rank_top_min:
            tier = RankingLevel.TOP
        elif score >= cfg.rank_high_min:
            tier = RankingLevel.HIGH
        elif score >= cfg.rank_medium_min:
            tier = RankingLevel.MEDIUM
        else:
            tier = RankingLevel.LOW

        # ── Hard overrides that cannot be outscored (§11.3) ──
        # Cross-chain never TOP unless bridge time < fast-bridge threshold (§7.5).
        if inp.arb_type == ArbitrageType.CROSS_CHAIN and tier == RankingLevel.TOP:
            fast = inp.bridge_time_sec is not None and (
                inp.bridge_time_sec < cfg.fast_bridge_threshold_sec
            )
            if not fast:
                tier = RankingLevel.HIGH
        # Not-yet-warmed-up market can never exceed MEDIUM (§3.1 / §11.3).
        if not inp.warmed_up and tier in (RankingLevel.TOP, RankingLevel.HIGH):
            tier = RankingLevel.MEDIUM
        return tier
