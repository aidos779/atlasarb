from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook
from src.scanner.profit.engine import ProfitEngine
from src.scanner.profit.liquidity_leg import CexBookLeg
from src.scanner.profit.models import FeeInputs


def _book(bid, ask, qty="1000"):
    sym = CanonicalSymbol("ETH", "USDT", VenueType.CEX)
    return OrderBook("v", sym, [BookLevel(Decimal(bid), Decimal(qty))],
                     [BookLevel(Decimal(ask), Decimal(qty))])


def test_net_profit_positive_after_fees():
    cfg = ScannerConfig()
    engine = ProfitEngine(cfg)
    buy = CexBookLeg(_book("2999", "3000"), "buy")
    sell = CexBookLeg(_book("3050", "3051"), "sell")
    fees = FeeInputs(Decimal("0.001"), Decimal("0.001"), Decimal("1"), Decimal("0"),
                     None, requires_withdrawal=True, requires_gas=False, requires_bridge=False)
    result = engine.optimize(buy, sell, fees, "CEX", "CEX")
    assert result is not None
    bd, sizing = result
    assert bd.net_profit_usd > 0
    assert sizing.recommended_size_usd > 0
    assert sizing.max_capital_utilization_usd >= sizing.recommended_size_usd


def test_all_fee_categories_resolved():
    fees = FeeInputs(Decimal("0.001"), Decimal("0.001"), Decimal("1"), Decimal("2"),
                     Decimal("3"), requires_withdrawal=True, requires_gas=True,
                     requires_bridge=True)
    assert fees.fully_resolved() is True


def test_missing_gas_is_unresolved():
    fees = FeeInputs(Decimal("0.001"), Decimal("0.001"), Decimal("1"), None, None,
                     requires_withdrawal=True, requires_gas=True, requires_bridge=False)
    assert "gas" not in fees.resolved_categories()
    assert fees.fully_resolved() is False
