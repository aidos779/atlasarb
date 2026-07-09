"""Signal Assembler — the pipeline that turns a raw Candidate (§7) into a validated,
ranked Signal (§8→§9→§10→§11), or a reject reason.

Kept separate from the engine's scheduling concerns (SRP): the engine decides *when*
to run detection; the assembler decides *whether* a candidate becomes a Signal and
*what* its numbers are. Depends only on ports (adapters/gas provider) — ARCH-1.
"""
from __future__ import annotations

import time
from decimal import Decimal

from src.config import get_logger
from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, RejectReason
from src.domain.market import OrderBook
from src.domain.ports import ExchangeAdapter, GasPriceProvider
from src.domain.signal import Candidate, Signal
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.liquidity.analyzer import LiquidityAnalyzer
from src.scanner.priority.scheduler import PriorityClassifier, profit_reference_for
from src.scanner.profit.engine import ProfitEngine
from src.scanner.profit.liquidity_leg import (
    CexBookLeg,
    DexPoolLeg,
    LiquidityLeg,
)
from src.scanner.profit.models import FeeInputs
from src.scanner.ranking.confidence import ConfidenceInputs, ConfidenceScorer
from src.scanner.ranking.ranker import RankingEngine, RankInputs, RiskInputs, classify_risk
from src.scanner.status.health_registry import HealthRegistry
from src.scanner.validation.validator import (
    SignalValidator,
    ValidationContext,
)

log = get_logger("scanner.assembler")

_GAS_UNITS_PER_SWAP = 150_000
_DEFAULT_HISTORICAL_RELIABILITY = Decimal(60)


class AssemblyResult:
    __slots__ = ("signal", "reject_reason")

    def __init__(self, signal: Signal | None, reject_reason: RejectReason | None) -> None:
        self.signal = signal
        self.reject_reason = reject_reason


class SignalAssembler:
    def __init__(
        self, config: ScannerConfig, cache: MarketStateCache, health: HealthRegistry,
        adapters: dict[str, ExchangeAdapter], gas: GasPriceProvider,
        priority: PriorityClassifier,
        reliability_provider=None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._health = health
        self._adapters = adapters
        self._gas = gas
        self._priority = priority
        self._profit = ProfitEngine(config)
        self._liquidity = LiquidityAnalyzer(config)
        self._confidence = ConfidenceScorer(config)
        self._ranker = RankingEngine(config)
        self._reliability = reliability_provider
        # Cached immutable venue taker-fee *rates* (§8.3). taker_fee() is a per-venue
        # constant for every current adapter, so memoize it once and reuse it in the
        # hot-loop pre-gate below instead of reconstructing a CanonicalSymbol and
        # re-dispatching per candidate (thousands of candidates/sec).
        self._fee_rate_cache: dict[str, Decimal] = {}

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config
        for comp in (self._profit, self._liquidity, self._confidence, self._ranker):
            comp.update_config(config)

    def _fee_rate(self, venue: str) -> Decimal | None:
        """Memoized taker-fee rate for a venue (fraction, e.g. 0.001). None if the
        adapter is unknown — caller then skips the pre-gate and takes the full path."""
        cached = self._fee_rate_cache.get(venue)
        if cached is not None:
            return cached
        adapter = self._adapters.get(venue)
        if adapter is None:
            return None
        from src.domain.enums import VenueType
        from src.domain.market import CanonicalSymbol
        vt = VenueType.DEX if adapter.venue_type == VenueType.DEX else VenueType.CEX
        rate = adapter.taker_fee(CanonicalSymbol("_", "_", vt))
        self._fee_rate_cache[venue] = rate
        return rate

    def _fee_floor_pct(self, cand: Candidate) -> Decimal | None:
        """Lower bound (in %) on the cost the spread must clear for any net profit.

        Uses only the size-independent components — the round-trip taker-fee *rate*
        and the stablecoin cross-quote conversion — both of which the profit engine
        also charges. Every omitted cost (slippage/withdrawal/gas/bridge) is >= 0, so
        this stays a strict lower bound: a candidate skipped here is guaranteed to
        fail downstream too. Returns None (skip the pre-gate) if either venue's fee
        rate is unknown, so nothing is ever wrongly rejected."""
        buy_rate = self._fee_rate(cand.buy_leg.venue)
        sell_rate = self._fee_rate(cand.sell_leg.venue)
        if buy_rate is None or sell_rate is None:
            return None
        floor = (buy_rate + sell_rate) * Decimal(100)
        if cand.quote_asset == "cross":
            floor += Decimal(str(self._config.stablecoin_crossquote_bps)) / Decimal(100)
        return floor

    async def assemble(self, cand: Candidate) -> AssemblyResult:
        if cand.arb_type == ArbitrageType.FUNDING:
            return await self._assemble_funding(cand)
        return await self._assemble_spot(cand)

    # ── spot / DEX / cross-chain path ──
    async def _assemble_spot(self, cand: Candidate) -> AssemblyResult:
        # ── cheap fee-rate pre-gate (§8 early-exit floor) ──────────────────────
        # net_pct is bounded above by gross_spread_pct minus the trading-fee rate
        # and the (size-independent) conversion cost — every other cost (slippage,
        # withdrawal, gas, bridge) only lowers it further. So if the spread cannot
        # even clear that floor, the candidate is provably a BELOW_MIN_PROFIT /
        # UNPROFITABLE_AFTER_FEES reject at *every* size. Reject it here, before the
        # expensive book fetch + 4-point sizing sweep + liquidity/confidence/ranking/
        # validation pipeline. This is exact — it only skips candidates that could
        # never publish — so detection accuracy and the set of valid signals are
        # unchanged; it just stops burning CPU on the ~40k/45k doomed candidates.
        floor = self._fee_floor_pct(cand)
        if floor is not None:
            gross = cand.gross_spread_pct
            if gross <= floor:
                return AssemblyResult(None, RejectReason.UNPROFITABLE_AFTER_FEES)
            if gross - floor < Decimal(str(self._config.min_roi_pct)):
                return AssemblyResult(None, RejectReason.BELOW_MIN_PROFIT)

        buy_book = self._book_for(cand.buy_leg.venue, cand)
        sell_book = self._book_for(cand.sell_leg.venue, cand)
        if buy_book is None or sell_book is None:
            return AssemblyResult(None, RejectReason.STALE_DATA)

        buy_leg = self._leg(buy_book, cand.buy_leg.venue_type, "buy")
        sell_leg = self._leg(sell_book, cand.sell_leg.venue_type, "sell")

        fees = await self._resolve_fees(cand)
        result = self._profit.optimize(
            buy_leg, sell_leg, fees, cand.buy_leg.venue_type, cand.sell_leg.venue_type
        )
        if result is None:
            return AssemblyResult(None, RejectReason.NO_VIABLE_SIZE)
        breakdown, sizing = result

        priority = self._priority.priority(cand.base_asset)
        reference = profit_reference_for(priority)
        liquidity_usd = self._liquidity.liquidity_usd(
            buy_leg, sell_leg, cand.buy_leg.venue_type, cand.sell_leg.venue_type
        )
        liq_score = self._liquidity.score(
            buy_leg, sell_leg, cand.buy_leg.venue_type, cand.sell_leg.venue_type, reference
        )

        warmed = self._warmed(cand)
        risk = classify_risk(RiskInputs(
            arb_type=cand.arb_type, liquidity_usd=liquidity_usd,
            liquidity_floor=Decimal(str(self._config.min_liquidity_usd(
                self._floor_venue_type(cand)))),
            cross_network_transfer=cand.arb_type in (
                ArbitrageType.CROSS_CHAIN, ArbitrageType.CEX_DEX),
            bridge_time_sec=cand.bridge_time_sec, atomic_execution=cand.atomic_execution,
            price_source_count=len(self._cache.price_window(
                cand.buy_leg.venue, f"{cand.base_asset}/{cand.quote_asset}")),
        ))

        conf = self._confidence.score(self._confidence_inputs(
            cand, buy_book, sell_book, liq_score, breakdown, warmed))

        rank_score = self._ranker.composite_score(RankInputs(
            net_profit_usd=breakdown.net_profit_usd, roi_pct=breakdown.roi_pct,
            profit_reference_usd=reference, liquidity_score=liq_score,
            confidence_score=Decimal(conf), risk=risk, arb_type=cand.arb_type,
            bridge_time_sec=cand.bridge_time_sec, warmed_up=warmed,
        ))
        tier = self._ranker.assign_tier(rank_score, RankInputs(
            net_profit_usd=breakdown.net_profit_usd, roi_pct=breakdown.roi_pct,
            profit_reference_usd=reference, liquidity_score=liq_score,
            confidence_score=Decimal(conf), risk=risk, arb_type=cand.arb_type,
            bridge_time_sec=cand.bridge_time_sec, warmed_up=warmed,
        ))

        vctx = ValidationContext(
            breakdown=breakdown, sizing=sizing, liquidity_usd=liquidity_usd,
            liquidity_score=liq_score, confidence_score=conf,
            buy_status=self._health.status(cand.buy_leg.venue),
            sell_status=self._health.status(cand.sell_leg.venue),
            max_data_staleness_sec=max(buy_book.staleness(), sell_book.staleness()),
            # DEX pool data refreshes per block and is spec-fresh up to
            # max_age_dex_price_sec (§16); judging a DEX leg against the tighter CEX
            # order-book age wrongly rejected valid DEX / CEX-DEX / cross-chain
            # opportunities as STALE_DATA.
            max_allowed_staleness_sec=(
                self._config.max_age_dex_price_sec
                if self._floor_venue_type(cand) == "DEX"
                else self._config.max_age_orderbook_cex_sec),
            venue_type_for_floor=self._floor_venue_type(cand),
            token_verified=True,  # detector already gated DEX tokens
            warmed_up=warmed, gas_fee_usd=breakdown.gas_fees_usd,
            gross_profit_usd=breakdown.gross_profit_usd,
            bridge_time_sec=cand.bridge_time_sec,
            is_cross_chain=cand.arb_type == ArbitrageType.CROSS_CHAIN,
            has_bridge_route=cand.bridge_name is not None
            if cand.arb_type == ArbitrageType.CROSS_CHAIN else True,
        )
        verdict = SignalValidator(self._config).validate(vctx)
        if not verdict.ok:
            self._log_rejection(cand, breakdown, sizing, verdict.reason)
            return AssemblyResult(None, verdict.reason)

        signal = self._build_signal(cand, breakdown, sizing, liquidity_usd, risk, conf,
                                    tier, rank_score, warmed)
        return AssemblyResult(signal, None)

    # ── funding path (§7.4 / §8.10) ──
    async def _assemble_funding(self, cand: Candidate) -> AssemblyResult:
        buy_ad = self._adapters.get(cand.buy_leg.venue)
        sell_ad = self._adapters.get(cand.sell_leg.venue)
        if buy_ad is None or sell_ad is None:
            return AssemblyResult(None, RejectReason.STALE_DATA)
        size = Decimal(str(self._config.funding_position_size_usd))
        annualized = cand.funding_annualized_spread or Decimal(0)
        # Funding arbitrage is a delta-neutral *carry* held across many settlement
        # intervals — the differential is collected every interval while entry+exit
        # trading fees are paid once. Projecting one day of funding against a full
        # round-trip fee (the previous model) made even a 40%+ annualized spread
        # look unprofitable. Project the carry over a realistic holding horizon
        # (§7.4) and charge the one-time round-trip fee against it.
        hold_fraction = Decimal(str(self._config.funding_hold_hours)) / Decimal(24 * 365)
        gross = size * annualized * hold_fraction
        from src.domain.enums import VenueType
        from src.domain.market import CanonicalSymbol
        sym = CanonicalSymbol(cand.base_asset, cand.quote_asset, VenueType.CEX)
        fees_usd = size * (buy_ad.taker_fee(sym) + sell_ad.taker_fee(sym))
        net = gross - fees_usd
        if net <= 0:
            return AssemblyResult(None, RejectReason.UNPROFITABLE_AFTER_FEES)
        # capitalDeployed = both legs' margin (§8.10); assume 2x notional hedged.
        capital = size * Decimal(2)
        roi = net / capital * Decimal(100)
        if net < Decimal(str(self._config.min_net_profit_usd)):
            return AssemblyResult(None, RejectReason.BELOW_MIN_PROFIT)
        from src.domain.signal import ProfitBreakdown, SizingProfile
        breakdown = ProfitBreakdown(
            size_usd=size, gross_profit_usd=gross, trading_fees_usd=fees_usd,
            withdrawal_fees_usd=Decimal(0), gas_fees_usd=Decimal(0),
            bridge_fees_usd=Decimal(0), slippage_cost_usd=Decimal(0),
            conversion_cost_usd=Decimal(0), net_profit_usd=net, roi_pct=roi,
            gross_spread_pct=cand.gross_spread_pct, net_profit_pct=roi,
            resolved_categories=frozenset(
                {"trading", "withdrawal", "gas", "bridge", "slippage"}),
        )
        sizing = SizingProfile(size, capital, size, capital, [(Decimal(100), roi)])
        conf = 75
        risk = classify_risk(RiskInputs(
            arb_type=ArbitrageType.FUNDING, liquidity_usd=capital,
            liquidity_floor=Decimal(str(self._config.min_liquidity_cex_usd)),
            cross_network_transfer=False, bridge_time_sec=None,
            atomic_execution=False,
        ))
        reference = profit_reference_for(self._priority.priority(cand.base_asset))
        rank_inputs = RankInputs(
            net_profit_usd=net, roi_pct=roi, profit_reference_usd=reference,
            liquidity_score=Decimal(70), confidence_score=Decimal(conf), risk=risk,
            arb_type=ArbitrageType.FUNDING, bridge_time_sec=None, warmed_up=True,
            funding_projected_profit=net,
        )
        score = self._ranker.composite_score(rank_inputs)
        tier = self._ranker.assign_tier(score, rank_inputs)
        signal = self._build_signal(cand, breakdown, sizing, capital, risk, conf,
                                    tier, score, True)
        signal.funding_annualized_spread = annualized
        signal.funding_next_time = cand.funding_next_time
        return AssemblyResult(signal, None)

    def _log_rejection(self, cand: Candidate, bd, sizing, reason) -> None:
        """Full per-candidate diagnostic (Scanner §17): every fee as a % of notional,
        gross/net %, and the required minimums, so a reject can be explained by real
        numbers rather than guesswork."""
        n = sizing.recommended_size_usd or Decimal(1)

        def pct(x: Decimal) -> float:
            return float(round(x / n * Decimal(100), 4))

        log.debug(
            "signal_rejected",
            reason=reason.value if reason else None,
            coin=cand.base_asset, arb_type=cand.arb_type.value,
            buy_venue=cand.buy_leg.venue, sell_venue=cand.sell_leg.venue,
            buy_price=float(cand.buy_leg.price), sell_price=float(cand.sell_leg.price),
            size_usd=float(round(n, 2)),
            gross_pct=pct(bd.gross_profit_usd),
            trading_pct=pct(bd.trading_fees_usd),
            withdrawal_pct=pct(bd.withdrawal_fees_usd),
            gas_pct=pct(bd.gas_fees_usd),
            bridge_pct=pct(bd.bridge_fees_usd),
            slippage_pct=pct(bd.slippage_cost_usd),
            conversion_pct=pct(bd.conversion_cost_usd),
            net_pct=float(round(bd.net_profit_pct, 4)),
            net_usd=float(round(bd.net_profit_usd, 2)),
            required_min_net_usd=self._config.min_net_profit_usd,
            required_min_roi_pct=self._config.min_roi_pct,
        )

    # ── helpers ──
    def _floor_venue_type(self, cand: Candidate) -> str:
        return "DEX" if "DEX" in (cand.buy_leg.venue_type, cand.sell_leg.venue_type) else "CEX"

    def _book_for(self, venue: str, cand: Candidate) -> OrderBook | None:
        return self._cache.get_book(venue, f"{cand.base_asset}/{cand.quote_asset}")

    def _leg(self, book: OrderBook, venue_type: str, side: str) -> LiquidityLeg:
        return DexPoolLeg(book, side) if venue_type == "DEX" else CexBookLeg(book, side)

    def _warmed(self, cand: Candidate) -> bool:
        pair = f"{cand.base_asset}/{cand.quote_asset}"
        return (self._cache.is_warmed_up(cand.buy_leg.venue, pair)
                and self._cache.is_warmed_up(cand.sell_leg.venue, pair))

    async def _resolve_fees(self, cand: Candidate) -> FeeInputs:
        from src.domain.enums import VenueType
        from src.domain.market import CanonicalSymbol
        buy_ad = self._adapters.get(cand.buy_leg.venue)
        sell_ad = self._adapters.get(cand.sell_leg.venue)
        buy_type = VenueType(cand.buy_leg.venue_type)
        sell_type = VenueType(cand.sell_leg.venue_type)
        buy_sym = CanonicalSymbol(cand.base_asset, cand.quote_asset, buy_type,
                                  cand.buy_leg.network)
        sell_sym = CanonicalSymbol(cand.base_asset, cand.quote_asset, sell_type,
                                   cand.sell_leg.network)
        buy_fee = buy_ad.taker_fee(buy_sym) if buy_ad else None
        sell_fee = sell_ad.taker_fee(sell_sym) if sell_ad else None

        arb = cand.arb_type
        requires_withdrawal = arb in (ArbitrageType.CEX_CEX, ArbitrageType.CEX_DEX)
        requires_gas = "DEX" in (cand.buy_leg.venue_type, cand.sell_leg.venue_type)
        requires_bridge = arb == ArbitrageType.CROSS_CHAIN

        withdrawal_usd: Decimal | None = Decimal(0)
        if requires_withdrawal and buy_ad is not None:
            withdrawal_usd = buy_ad.withdrawal_fee_usd(cand.base_asset, cand.buy_leg.network)

        gas_usd: Decimal | None = Decimal(0)
        if requires_gas:
            gas_usd = await self._gas_estimate(cand)

        bridge_usd: Decimal | None = None
        if requires_bridge:
            bridge_usd = cand.bridge_fee_usd

        return FeeInputs(
            buy_fee_rate=buy_fee, sell_fee_rate=sell_fee,
            withdrawal_fee_usd=withdrawal_usd, gas_fee_usd=gas_usd,
            bridge_fee_usd=bridge_usd,
            conversion_cost_bps=(Decimal(str(self._config.stablecoin_crossquote_bps))
                                 if cand.quote_asset == "cross" else Decimal(0)),
            requires_withdrawal=requires_withdrawal, requires_gas=requires_gas,
            requires_bridge=requires_bridge,
        )

    async def _gas_estimate(self, cand: Candidate) -> Decimal | None:
        networks = [n for n in (cand.buy_leg.network, cand.sell_leg.network) if n]
        if not networks:
            return Decimal(0)
        total = Decimal(0)
        for network in networks:
            g = await self._gas.gas_price_usd(network, _GAS_UNITS_PER_SWAP)
            if g is None:
                return None  # unresolved -> validator rejects
            total += g
        return total

    def _confidence_inputs(self, cand, buy_book, sell_book, liq_score, breakdown,
                           warmed) -> ConfidenceInputs:
        max_age = self._config.max_age_orderbook_cex_sec
        fresh = min(
            Decimal(1) - Decimal(str(min(buy_book.staleness(), max_age))) / Decimal(str(max_age)),
            Decimal(1) - Decimal(str(min(sell_book.staleness(), max_age))) / Decimal(str(max_age)),
        ) * Decimal(100)
        pair = f"{cand.base_asset}/{cand.quote_asset}"
        window = self._cache.price_window(cand.buy_leg.venue, pair)
        stability = min(Decimal(100),
                        Decimal(len(window)) / Decimal(self._config.outlier_window_ticks)
                        * Decimal(100))
        health = min(self._health.health_factor(cand.buy_leg.venue),
                     self._health.health_factor(cand.sell_leg.venue))
        reliability = _DEFAULT_HISTORICAL_RELIABILITY
        if self._reliability is not None:
            reliability = self._reliability(cand.arb_type.value, cand.buy_leg.venue,
                                            cand.sell_leg.venue)
        required = 5
        completeness = Decimal(len(breakdown.resolved_categories)) / Decimal(required) * Decimal(100)
        return ConfidenceInputs(
            price_freshness=max(Decimal(0), fresh), liquidity_score=liq_score,
            spread_stability=stability, exchange_health=health,
            historical_reliability=reliability, data_completeness=min(Decimal(100), completeness),
        )

    def _build_signal(self, cand, breakdown, sizing, liquidity_usd, risk, conf, tier,
                      score, warmed) -> Signal:
        now = time.time()
        return Signal(
            arb_type=cand.arb_type, coin=cand.base_asset,
            trading_pair=f"{cand.base_asset}/{cand.quote_asset}", network=cand.network,
            buy_exchange=cand.buy_leg.venue, sell_exchange=cand.sell_leg.venue,
            buy_price=cand.buy_leg.price, sell_price=cand.sell_leg.price,
            buy_venue_type=cand.buy_leg.venue_type, sell_venue_type=cand.sell_leg.venue_type,
            pool_address=cand.sell_leg.pool_address or cand.buy_leg.pool_address,
            spread_pct=breakdown.gross_spread_pct, gross_profit_pct=breakdown.gross_spread_pct,
            net_profit_pct=breakdown.net_profit_pct, net_profit_usd=breakdown.net_profit_usd,
            trading_fees=breakdown.trading_fees_usd, withdrawal_fees=breakdown.withdrawal_fees_usd,
            network_fees=breakdown.gas_fees_usd, estimated_slippage=breakdown.slippage_cost_usd,
            conversion_cost=breakdown.conversion_cost_usd, liquidity_usd=liquidity_usd,
            recommended_trade_size_usd=sizing.recommended_size_usd, roi_pct=breakdown.roi_pct,
            sizing=sizing, profit_breakdown=breakdown, risk_score=risk, confidence_score=conf,
            ranking=tier, composite_score=score,
            timestamp=now, last_updated=now, bridge_name=cand.bridge_name,
            bridge_time_sec=cand.bridge_time_sec, atomic_execution=cand.atomic_execution,
            warmed_up=warmed,
            price_source_count=len(self._cache.price_window(
                cand.buy_leg.venue, f"{cand.base_asset}/{cand.quote_asset}")),
        )
