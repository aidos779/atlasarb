from decimal import Decimal

from src.domain.entitlements import entitlements_for
from src.domain.enums import ArbitrageType, RiskScore, SubscriptionTier
from src.domain.signal import Signal
from src.domain.user import UserFilter


def test_free_tier_caps():
    free = entitlements_for(SubscriptionTier.FREE)
    assert free.signals_per_refresh == 5
    assert free.max_favorite_coins == 3
    assert not free.arb_type_allowed(ArbitrageType.CEX_DEX)
    assert not free.filter_allowed("network")
    assert free.can_add_favorite("coin", 2) is True
    assert free.can_add_favorite("coin", 3) is False


def test_pro_tier_unlimited():
    pro = entitlements_for(SubscriptionTier.PRO)
    assert pro.can_add_favorite("coin", 9999) is True
    assert pro.arb_type_allowed(ArbitrageType.CROSS_CHAIN)
    assert pro.filter_allowed("risk")


def _signal(**kw):
    base = dict(coin="ETH", trading_pair="ETH/USDT", net_profit_pct=Decimal("1.5"),
                liquidity_usd=Decimal("50000"), risk_score=RiskScore.LOW,
                buy_exchange="binance", sell_exchange="okx", arb_type=ArbitrageType.CEX_CEX)
    base.update(kw)
    return Signal(**base)


def test_filter_min_profit():
    f = UserFilter(min_profit_pct=Decimal("2.0"))
    assert not f.matches(_signal(net_profit_pct=Decimal("1.5")))
    assert f.matches(_signal(net_profit_pct=Decimal("2.5")))


def test_filter_coin_and_exchange_and_logic():
    f = UserFilter(coins=frozenset({"ETH"}), exchanges=frozenset({"binance"}))
    assert f.matches(_signal())
    assert not f.matches(_signal(coin="BTC"))
    assert not f.matches(_signal(buy_exchange="mexc", sell_exchange="okx"))


def test_filter_risk_threshold():
    f = UserFilter(max_risk_numeric=1)
    assert f.matches(_signal(risk_score=RiskScore.LOW))
    assert not f.matches(_signal(risk_score=RiskScore.HIGH))
