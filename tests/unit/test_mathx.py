from decimal import Decimal

from src.domain.market import BookLevel
from src.scanner import mathx


def test_vwap_fill_price_walks_book():
    levels = [BookLevel(Decimal("100"), Decimal("5")),
              BookLevel(Decimal("102"), Decimal("10"))]
    # Fill 10 units: 5@100 + 5@102 = (500+510)/10 = 101
    assert mathx.vwap_fill_price(levels, Decimal("10")) == Decimal("101")


def test_vwap_insufficient_depth_returns_none():
    levels = [BookLevel(Decimal("100"), Decimal("1"))]
    assert mathx.vwap_fill_price(levels, Decimal("5")) is None


def test_amm_output_constant_product():
    # x*y=k; swapping into a huge pool gives near-spot output.
    out = mathx.amm_output(Decimal("1000000"), Decimal("1000000"),
                           Decimal("1000"), Decimal("0"))
    assert Decimal("998") < out < Decimal("1000")


def test_amm_effective_price_worse_for_larger_size():
    small = mathx.amm_effective_price(Decimal("1000000"), Decimal("500"),
                                      Decimal("100"), Decimal("0.003"))
    large = mathx.amm_effective_price(Decimal("1000000"), Decimal("500"),
                                      Decimal("50000"), Decimal("0.003"))
    assert large > small  # size-aware pricing gets worse (higher in per out)


def test_mad_outlier_detection():
    window = [Decimal("100")] * 20
    assert mathx.is_mad_outlier(Decimal("100.01"), window, Decimal("5")) is True  # zero MAD
    varied = [Decimal(str(100 + (i % 3))) for i in range(20)]
    assert mathx.is_mad_outlier(Decimal("100"), varied, Decimal("5")) is False
    assert mathx.is_mad_outlier(Decimal("500"), varied, Decimal("5")) is True


def test_max_fillable_within_slippage():
    levels = [BookLevel(Decimal("100"), Decimal("10")),
              BookLevel(Decimal("105"), Decimal("10"))]
    size = mathx.max_fillable_within_slippage(levels, Decimal("100"), Decimal("1"))
    assert size >= Decimal("10")  # first level within 1% slippage entirely
