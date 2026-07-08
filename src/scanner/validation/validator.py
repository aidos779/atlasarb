"""Signal Validator (Scanner §10). Every gate must pass; any failure = reject+log.

Order mirrors the §10 flowchart: min profit -> liquidity -> fees -> risk -> freshness
-> exchange status -> confidence, plus the §15.5 implausible-spread last-resort ceiling
and token/bridge/gas/warm-up/size hard floors.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.config import get_logger
from src.config.scanner_config import ScannerConfig
from src.domain.enums import ExchangeStatus, RejectReason
from src.domain.signal import ProfitBreakdown, SizingProfile

log = get_logger("scanner.validator")


@dataclass
class ValidationContext:
    breakdown: ProfitBreakdown
    sizing: SizingProfile
    liquidity_usd: Decimal
    liquidity_score: Decimal
    confidence_score: int
    buy_status: ExchangeStatus
    sell_status: ExchangeStatus
    max_data_staleness_sec: float
    max_allowed_staleness_sec: float
    venue_type_for_floor: str
    token_verified: bool
    warmed_up: bool
    gas_fee_usd: Decimal
    gross_profit_usd: Decimal
    bridge_time_sec: int | None = None
    is_cross_chain: bool = False
    has_bridge_route: bool = True


@dataclass
class ValidationResult:
    ok: bool
    reason: RejectReason | None = None

    @classmethod
    def passed(cls) -> ValidationResult:
        return cls(ok=True)

    @classmethod
    def reject(cls, reason: RejectReason) -> ValidationResult:
        return cls(ok=False, reason=reason)


class SignalValidator:
    def __init__(self, config: ScannerConfig) -> None:
        self._config = config

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def validate(self, ctx: ValidationContext) -> ValidationResult:
        cfg = self._config
        bd = ctx.breakdown

        # §15.5 last-resort implausible-spread ceiling (defense in depth).
        if bd.gross_spread_pct > Decimal(str(cfg.implausible_spread_pct)):
            return self._fail(RejectReason.IMPLAUSIBLE_SPREAD)

        # Risk hard floor: unverified token = automatic reject (§10 Risk gate).
        if not ctx.token_verified:
            return self._fail(RejectReason.UNVERIFIED_TOKEN)

        # Cross-chain route existence (§7.5) + bridge time ceiling.
        if ctx.is_cross_chain:
            if not ctx.has_bridge_route:
                return self._fail(RejectReason.NO_BRIDGE_ROUTE)
            if ctx.bridge_time_sec is not None and ctx.bridge_time_sec > cfg.max_bridge_time_sec:
                return self._fail(RejectReason.BRIDGE_TIME_EXCEEDED)

        # Gate 1 — Minimum Profit (both bars, §10).
        if (bd.net_profit_usd < Decimal(str(cfg.min_net_profit_usd))
                or bd.roi_pct < Decimal(str(cfg.min_roi_pct))):
            return self._fail(RejectReason.BELOW_MIN_PROFIT)

        # Gate 2 — Liquidity (score floor + min tradeable size + floor value).
        floor = Decimal(str(cfg.min_liquidity_usd(ctx.venue_type_for_floor)))
        if ctx.liquidity_usd < floor:
            return self._fail(RejectReason.INSUFFICIENT_LIQUIDITY)
        if ctx.liquidity_score < Decimal(str(cfg.liquidity_score_floor)):
            return self._fail(RejectReason.INSUFFICIENT_LIQUIDITY)
        if ctx.sizing.recommended_size_usd < Decimal(str(cfg.min_tradeable_size_usd)):
            return self._fail(RejectReason.INSUFFICIENT_LIQUIDITY)
        if ctx.sizing.max_recommended_position_usd < Decimal(str(cfg.min_tradeable_size_usd)):
            # §8.12 — no viable profitable size.
            return self._fail(RejectReason.NO_VIABLE_SIZE)

        # Gate 3 — Fees fully accounted and net-positive.
        required = {"trading", "withdrawal", "gas", "bridge", "slippage"}
        if not required.issubset(bd.resolved_categories):
            return self._fail(RejectReason.MISSING_FEE_DATA)
        if bd.net_profit_usd <= 0:
            return self._fail(RejectReason.UNPROFITABLE_AFTER_FEES)

        # §7.2 gas-vs-gross ceiling.
        if ctx.gross_profit_usd > 0:
            gas_pct = ctx.gas_fee_usd / ctx.gross_profit_usd * Decimal(100)
            if gas_pct > Decimal(str(cfg.max_gas_pct_of_gross)):
                return self._fail(RejectReason.GAS_EXCEEDS_LIMIT)

        # Gate 4 — Risk within bounds (warm-up already capped at ranking; hard floor here).
        if not ctx.warmed_up:
            return self._fail(RejectReason.NOT_WARMED_UP)

        # Gate 5 — Freshness (re-checked at emission time).
        if ctx.max_data_staleness_sec > ctx.max_allowed_staleness_sec:
            return self._fail(RejectReason.STALE_DATA)

        # Gate 6 — Exchange status (both Online, defense in depth §14.3).
        if not (ctx.buy_status.signal_allowed and ctx.sell_status.signal_allowed):
            return self._fail(RejectReason.EXCHANGE_NOT_ONLINE)

        # Gate 7 — Confidence floor.
        if ctx.confidence_score < cfg.confidence_threshold:
            return self._fail(RejectReason.LOW_CONFIDENCE)

        return ValidationResult.passed()

    def _fail(self, reason: RejectReason) -> ValidationResult:
        log.debug("candidate_rejected", reason=reason.value)
        return ValidationResult.reject(reason)
