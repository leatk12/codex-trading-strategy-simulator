"""Pure calculations for simulated execution prices and trading costs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import AssetProfile
from .domain import TradeSide


class ExecutionError(ValueError):
    """Raised when a requested simulated execution is financially invalid."""


@dataclass(frozen=True, slots=True)
class ExecutionQuote:
    """A non-mutating estimate of one buy or sell transaction."""

    side: TradeSide
    quantity: Decimal
    market_price: Decimal
    execution_price: Decimal
    gross_notional: Decimal
    fee: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    cash_amount: Decimal

    @property
    def total_costs(self) -> Decimal:
        return self.fee + self.spread_cost + self.slippage_cost


@dataclass(frozen=True, slots=True)
class TradingCostModel:
    """Deterministic per-side spread, slippage, and fee assumptions.

    `spread_rate` represents the full bid/ask spread. Each side therefore bears
    half. `slippage_rate` worsens each execution relative to the midpoint.
    """

    fee_rate: Decimal
    spread_rate: Decimal
    slippage_rate: Decimal

    def __post_init__(self) -> None:
        for name in ("fee_rate", "spread_rate", "slippage_rate"):
            rate = getattr(self, name)
            if not Decimal("0") <= rate <= Decimal("1"):
                raise ExecutionError(f"{name} must be between 0 and 1")
        if self._sell_execution_factor <= 0:
            raise ExecutionError("spread and slippage leave no positive sell price")
        if self.fee_rate >= 1:
            raise ExecutionError("fee_rate must be less than 1")

    @classmethod
    def from_profile(cls, profile: AssetProfile) -> "TradingCostModel":
        return cls(
            fee_rate=profile.estimated_fee_rate,
            spread_rate=profile.estimated_spread_rate,
            slippage_rate=profile.estimated_slippage_rate,
        )

    @property
    def _half_spread_rate(self) -> Decimal:
        return self.spread_rate / Decimal("2")

    @property
    def _buy_execution_factor(self) -> Decimal:
        return Decimal("1") + self._half_spread_rate + self.slippage_rate

    @property
    def _sell_execution_factor(self) -> Decimal:
        return Decimal("1") - self._half_spread_rate - self.slippage_rate

    def quote_buy_for_budget(
        self, market_price: Decimal, cash_budget: Decimal
    ) -> ExecutionQuote:
        """Spend exactly `cash_budget`, including execution costs and fee."""

        self._require_positive("market_price", market_price)
        self._require_positive("cash_budget", cash_budget)
        execution_price = market_price * self._buy_execution_factor
        all_in_price_per_unit = execution_price * (Decimal("1") + self.fee_rate)
        quantity = cash_budget / all_in_price_per_unit
        return self._buy_quote(market_price, execution_price, quantity)

    def quote_buy_quantity(
        self, market_price: Decimal, quantity: Decimal
    ) -> ExecutionQuote:
        self._require_positive("market_price", market_price)
        self._require_positive("quantity", quantity)
        execution_price = market_price * self._buy_execution_factor
        return self._buy_quote(market_price, execution_price, quantity)

    def _buy_quote(
        self,
        market_price: Decimal,
        execution_price: Decimal,
        quantity: Decimal,
    ) -> ExecutionQuote:
        gross = quantity * execution_price
        fee = gross * self.fee_rate
        spread_cost = quantity * market_price * self._half_spread_rate
        slippage_cost = quantity * market_price * self.slippage_rate
        return ExecutionQuote(
            side=TradeSide.BUY,
            quantity=quantity,
            market_price=market_price,
            execution_price=execution_price,
            gross_notional=gross,
            fee=fee,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            cash_amount=gross + fee,
        )

    def quote_sell(
        self, market_price: Decimal, quantity: Decimal
    ) -> ExecutionQuote:
        """Estimate net cash received for a sale after all selling costs."""

        self._require_positive("market_price", market_price)
        self._require_positive("quantity", quantity)
        execution_price = market_price * self._sell_execution_factor
        gross = quantity * execution_price
        fee = gross * self.fee_rate
        spread_cost = quantity * market_price * self._half_spread_rate
        slippage_cost = quantity * market_price * self.slippage_rate
        return ExecutionQuote(
            side=TradeSide.SELL,
            quantity=quantity,
            market_price=market_price,
            execution_price=execution_price,
            gross_notional=gross,
            fee=fee,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            cash_amount=gross - fee,
        )

    def break_even_market_price(self, all_in_entry_price: Decimal) -> Decimal:
        """Market midpoint required for a sale to recover all-in entry cost."""

        self._require_positive("all_in_entry_price", all_in_entry_price)
        net_sell_factor = self._sell_execution_factor * (
            Decimal("1") - self.fee_rate
        )
        return all_in_entry_price / net_sell_factor

    @staticmethod
    def _require_positive(name: str, value: Decimal) -> None:
        if value <= 0 or not value.is_finite():
            raise ExecutionError(f"{name} must be positive and finite")

