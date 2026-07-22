"""Phase 2 — Funding formatter redesign: data propagation + dedicated rendering.

Covers detector→candidate→assembler→signal propagation of the new presentation fields,
and the funding-specific alert / compact card / detailed card, including the units-
regression (annualized shown at full magnitude, not 1/100th) and the regressions against
the $1.00 placeholder and the spot "Buy → Sell / Move coins" template.
"""
from __future__ import annotations

import time
from decimal import Decimal

import pytest

from src.bot.formatters.signal import (
    _hold_horizon,
    format_alert,
    format_card,
    format_details,
)
from src.config.scanner_config import ScannerConfig
from src.domain.enums import (
    ArbitrageType,
    ExchangeStatus,
    Language,
    RiskScore,
    VenueType,
)
from src.domain.market import FundingRate
from src.domain.ports import ExchangeAdapter
from src.domain.signal import (
    Candidate,
    FundingSnapshot,
    LegRef,
    ProfitBreakdown,
    Signal,
    SizingProfile,
)
from src.domain.user import UserProfile
from src.i18n import LANGUAGES
from src.scanner.assembler import SignalAssembler
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.detectors.base import DetectionContext, VenueInfo
from src.scanner.detectors.funding import FundingDetector
from src.scanner.priority.scheduler import PriorityClassifier
from src.scanner.status.health_registry import HealthRegistry


# ── 1. detector propagation ─────────────────────────────────────────────────────────────
def _funding_ctx(cfg):
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    venues = {}
    for v in ("binance", "okx"):
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
        venues[v] = VenueInfo(id=v, venue_type=VenueType.CEX)
    ctx = DetectionContext(cache=cache, health=health, venues=venues,
                           funding_min_annualized_spread=Decimal("0.05"))
    return ctx, cache


def test_detector_retains_per_leg_annualized_in_percent():
    cfg = ScannerConfig()
    ctx, cache = _funding_ctx(cfg)
    now = time.time()
    # 8h interval → annualized = rate × 3 × 365. low=0.0001→10.95%, high=0.0005→54.75%.
    cache.upsert_funding(FundingRate("binance", "ETH", Decimal("0.0001"), None, now + 3600, 8))
    cache.upsert_funding(FundingRate("okx", "ETH", Decimal("0.0005"), None, now + 3600, 8))
    out = FundingDetector().detect(ctx, "ETH", "USDT")
    assert len(out) == 1
    cand = out[0]
    assert cand.funding_buy_annualized == Decimal("0.0001") * 3 * 365 * 100    # 10.95
    assert cand.funding_sell_annualized == Decimal("0.0005") * 3 * 365 * 100   # 54.75
    # sell − buy (percent) equals the emitted gross_spread_pct (percent).
    assert (cand.funding_sell_annualized - cand.funding_buy_annualized
            == cand.gross_spread_pct)
    # buy_exchange is the long/low leg, sell_exchange the short/high leg.
    assert cand.buy_leg.venue == "binance" and cand.sell_leg.venue == "okx"


# ── 2. assembler propagation ────────────────────────────────────────────────────────────
class _Ad(ExchangeAdapter):
    venue_type = VenueType.CEX
    network = None

    def __init__(self, vid):
        self.id = vid
        self.display_name = vid

    async def connect(self): ...
    async def disconnect(self): ...
    async def get_markets(self): return []
    async def subscribe_ticker(self, s): ...
    async def subscribe_order_book(self, s, d): ...
    async def get_funding_rate(self, a): return None
    async def get_pool_state(self, s): return None
    async def health_check(self): return True
    def get_status(self): return ExchangeStatus.ONLINE
    def taker_fee(self, s): return Decimal("0.001")
    def withdrawal_fee_usd(self, a, n): return Decimal("1")
    def withdrawals_enabled(self, a, n): return True


class _Gas:
    async def gas_price_usd(self, n, u): return Decimal("0")


async def test_assembler_propagates_funding_presentation_fields():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    adapters = {"binance": _Ad("binance"), "okx": _Ad("okx")}
    for v in adapters:
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
    now = time.time()
    cache.upsert_funding(FundingRate("binance", "ETH", Decimal("0"), None, now + 3600, 8))
    cache.upsert_funding(FundingRate("okx", "ETH", Decimal("0"), None, now + 3600, 8))
    asm = SignalAssembler(cfg, cache, health, adapters, _Gas(), PriorityClassifier(cfg))
    snap = FundingSnapshot(received_at=now, current_rate=Decimal("0"),
                           has_predicted=False, has_next_time=True, history=(Decimal("0"),))
    cand = Candidate(arb_type=ArbitrageType.FUNDING, base_asset="ETH", quote_asset="USDT",
                     buy_leg=LegRef("binance", "CEX", Decimal(1)),
                     sell_leg=LegRef("okx", "CEX", Decimal(1)),
                     gross_spread_pct=Decimal("45"),
                     funding_annualized_spread=Decimal("0.45"),
                     funding_buy_annualized=Decimal("10.95"),
                     funding_sell_annualized=Decimal("54.75"),
                     funding_next_time=now + 3600, funding_low=snap, funding_high=snap)
    res = await asm.assemble(cand)
    assert res.signal is not None
    assert res.signal.funding_buy_annualized == Decimal("10.95")
    assert res.signal.funding_sell_annualized == Decimal("54.75")
    assert res.signal.funding_hold_hours == cfg.funding_hold_hours   # 168.0


# ── formatter fixtures ──────────────────────────────────────────────────────────────────
class _Fx:
    async def convert(self, amount, currency): return amount
    async def rate(self, currency): return Decimal(1)


def _profile(lang: str = "en") -> UserProfile:
    p = UserProfile(telegram_user_id=1, username="t", first_name="T")
    p.settings.language = Language(lang)
    return p


def _fsig(next_time=None) -> Signal:
    return Signal(
        arb_type=ArbitrageType.FUNDING, coin="NEAR", trading_pair="NEAR/USDT", network=None,
        buy_exchange="okx", sell_exchange="bitget",
        buy_price=Decimal("1"), sell_price=Decimal("1"),
        buy_venue_type="CEX", sell_venue_type="CEX",
        spread_pct=Decimal("43.80"),               # annualized differential, PERCENT
        funding_annualized_spread=Decimal("0.438"),  # legacy fraction (must NOT be shown)
        net_profit_pct=Decimal("0.25"), net_profit_usd=Decimal("5.01"),
        liquidity_usd=Decimal("2000"), risk_score=RiskScore.LOW, confidence_score=76,
        funding_buy_annualized=Decimal("10.95"), funding_sell_annualized=Decimal("54.75"),
        funding_hold_hours=168.0,
        funding_next_time=next_time if next_time is not None else time.time() + 3600,
        profit_breakdown=ProfitBreakdown(
            size_usd=Decimal("1000"), gross_profit_usd=Decimal("7"),
            trading_fees_usd=Decimal("2"), withdrawal_fees_usd=Decimal("0"),
            gas_fees_usd=Decimal("0"), bridge_fees_usd=Decimal("0"),
            slippage_cost_usd=Decimal("0"), conversion_cost_usd=Decimal("0"),
            net_profit_usd=Decimal("5.01"), roi_pct=Decimal("0.25"),
            gross_spread_pct=Decimal("43.80"), net_profit_pct=Decimal("0.25")),
        sizing=SizingProfile(Decimal("1000"), Decimal("2000"), Decimal("1000"),
                             Decimal("2000"), [(Decimal("100"), Decimal("0.25"))]),
    )


# ── 3. alert (units regression) ─────────────────────────────────────────────────────────
async def test_funding_alert_units_and_no_spot_template():
    text = await format_alert(_fsig(), _profile(), _Fx())
    assert "+43.80%" in text          # full-magnitude annualized from spread_pct
    assert "+0.44%" not in text       # NOT the 1/100th fraction (the Phase-1 units bug)
    assert "Long okx" in text and "Short bitget" in text
    assert "$1.00" not in text
    assert "Buy okx" not in text and "Sell bitget" not in text


# ── 4. compact card ─────────────────────────────────────────────────────────────────────
async def test_funding_compact_card():
    text = await format_card(_fsig(), _profile(), _Fx())
    assert "Long: okx" in text and "Short: bitget" in text
    assert "+43.80%" in text
    assert "Buy: okx" not in text and "$1.00" not in text


async def test_spot_compact_card_unchanged():
    sig = _fsig()
    sig.arb_type = ArbitrageType.CEX_CEX
    text = await format_card(sig, _profile(), _Fx())
    assert "Buy: okx" in text and "Sell: bitget" in text


# ── 5. detailed card ────────────────────────────────────────────────────────────────────
async def test_funding_detailed_card_complete():
    text = await format_details(_fsig(), _profile(), _Fx())
    assert "Long (low funding) on okx" in text
    assert "Short (high funding) on bitget" in text
    # per-leg annualized rates (percent)
    assert "long +10.95%" in text and "short +54.75%" in text
    assert "Next funding" in text
    assert "Holding horizon" in text and "7 days" in text
    assert "Net over 7 days" in text
    # hedge execution guide (details_advanced is on for all tiers in this build)
    assert "Open LONG NEAR perpetual on okx" in text
    assert "Open SHORT NEAR perpetual on bitget" in text
    assert "~7 days" in text
    # regressions
    assert "$1.00" not in text
    assert "Buy on okx" not in text
    assert "Move NEAR" not in text


async def test_spot_detailed_card_keeps_prices():
    sig = _fsig()
    sig.arb_type = ArbitrageType.CEX_CEX
    sig.buy_price = Decimal("100")
    sig.sell_price = Decimal("102")
    text = await format_details(sig, _profile(), _Fx())
    assert "Buy on okx" in text


# ── 6. edge cases ───────────────────────────────────────────────────────────────────────
async def test_next_funding_in_the_past_renders_imminent():
    text = await format_details(_fsig(next_time=time.time() - 60), _profile(), _Fx())
    assert "imminent" in text


def test_hold_horizon_days_and_hours():
    assert _hold_horizon(168.0, "en") == "7 days"
    assert _hold_horizon(36.0, "en") == "36 hours"     # non-24-multiple → hours
    assert _hold_horizon(None, "en") is None
    assert _hold_horizon(168.0, "ru") == "7 дн."


# ── 7. localization ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lang", LANGUAGES)
async def test_funding_surfaces_localized_no_marker(lang):
    prof = _profile(lang)
    fx = _Fx()
    for text in (await format_alert(_fsig(), prof, fx),
                 await format_card(_fsig(), prof, fx),
                 await format_details(_fsig(), prof, fx)):
        assert "⟦" not in text     # no missing i18n key in any language


@pytest.mark.parametrize("lang", LANGUAGES)
def test_new_keys_present_in_every_language(lang):
    from src.i18n import t
    for key in ("card.funding_route", "details.funding_leg_rate", "details.funding_hold",
                "details.funding_horizon", "details.funding_next_now",
                "unit.days", "unit.hours", "route.funding_hold",
                "route.funding_hold_nohorizon"):
        assert "⟦" not in t(key, lang, long="", short="", annualized="", buy_rate="",
                            sell_rate="", days="", n=0)
