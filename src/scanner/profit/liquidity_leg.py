"""Per-leg liquidity abstraction — unifies CEX order-book and DEX AMM pool pricing.

The profit engine works against this abstraction so it never branches on venue type
(ARCH-1 / SRP). A leg answers two questions: (1) size-aware fill price for a given
USD size, (2) max USD size executable within a slippage tolerance (§8.7, §9.2).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from src.domain.market import OrderBook
from src.scanner import mathx


class LiquidityLeg(ABC):
    side: str  # "buy" | "sell"

    @abstractmethod
    def best_price(self) -> Decimal | None: ...

    @abstractmethod
    def fill_price(self, size_usd: Decimal) -> Decimal | None:
        """Size-aware execution price (quote per base) for `size_usd` notional."""

    @abstractmethod
    def max_size_usd(self, max_slippage_pct: Decimal) -> Decimal:
        """Max USD size before slippage exceeds tolerance (§9.2)."""

    @abstractmethod
    def liquidity_usd(self, max_slippage_pct: Decimal) -> Decimal:
        """Executable USD liquidity within tolerance (feeds §9 liquidity floor)."""


class CexBookLeg(LiquidityLeg):
    """CEX leg — walks the L2 book (VWAP method, §8.7)."""

    def __init__(self, book: OrderBook, side: str) -> None:
        self.book = book
        self.side = side
        self._levels = book.asks if side == "buy" else book.bids

    def best_price(self) -> Decimal | None:
        return self.book.best_ask if self.side == "buy" else self.book.best_bid

    def fill_price(self, size_usd: Decimal) -> Decimal | None:
        best = self.best_price()
        if not best or best <= 0:
            return None
        size_base = size_usd / best
        return mathx.vwap_fill_price(self._levels, size_base)

    def max_size_usd(self, max_slippage_pct: Decimal) -> Decimal:
        best = self.best_price()
        if not best:
            return Decimal(0)
        max_base = mathx.max_fillable_within_slippage(self._levels, best, max_slippage_pct)
        return max_base * best

    def liquidity_usd(self, max_slippage_pct: Decimal) -> Decimal:
        return self.max_size_usd(max_slippage_pct)


class DexPoolLeg(LiquidityLeg):
    """DEX AMM leg — closed-form constant-product pricing (§5.4)."""

    def __init__(self, book: OrderBook, side: str) -> None:
        self.book = book
        self.side = side
        self.fee_rate = book.pool_fee_tier or Decimal("0.003")
        # For a buy (spend quote, receive base): reserve_in=quote, reserve_out=base.
        # For a sell (spend base, receive quote): reserve_in=base, reserve_out=quote.
        self.reserve_base = book.reserve_base or Decimal(0)
        self.reserve_quote = book.reserve_quote or Decimal(0)

    def _spot(self) -> Decimal | None:
        if self.reserve_base <= 0:
            return None
        return self.reserve_quote / self.reserve_base  # quote per base

    def best_price(self) -> Decimal | None:
        return self._spot()

    def fill_price(self, size_usd: Decimal) -> Decimal | None:
        spot = self._spot()
        if not spot or spot <= 0:
            return None
        if self.side == "buy":
            amount_in_quote = size_usd  # quote ~ USD (USDT)
            base_out = mathx.amm_output(
                self.reserve_quote, self.reserve_base, amount_in_quote, self.fee_rate
            )
            if base_out <= 0:
                return None
            return amount_in_quote / base_out  # quote per base
        # sell
        base_in = size_usd / spot
        quote_out = mathx.amm_output(
            self.reserve_base, self.reserve_quote, base_in, self.fee_rate
        )
        if quote_out <= 0:
            return None
        return quote_out / base_in

    def max_size_usd(self, max_slippage_pct: Decimal) -> Decimal:
        spot = self._spot()
        if not spot:
            return Decimal(0)
        if self.side == "buy":
            max_quote = mathx.amm_max_size_within_slippage(
                self.reserve_quote, self.reserve_base, self.fee_rate, max_slippage_pct
            )
            return max_quote
        max_base = mathx.amm_max_size_within_slippage(
            self.reserve_base, self.reserve_quote, self.fee_rate, max_slippage_pct
        )
        return max_base * spot

    def liquidity_usd(self, max_slippage_pct: Decimal) -> Decimal:
        # DEX liquidity metric = tradeable value within tolerance (§5.4).
        return self.max_size_usd(max_slippage_pct)
