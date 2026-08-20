"""Core records shared by strategies, backtests, and future adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping


class MarketState(str, Enum):
    NORMAL = "normal"
    UPTREND = "uptrend"
    EXPLOSIVE_MOMENTUM = "explosive_momentum"
    DECLINING = "declining"
    STABILISING = "stabilising"
    RECOVERING = "recovering"
    STRUCTURAL_BREAKDOWN = "structural_breakdown"
    MANUAL_REVIEW = "manual_review"


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    SUSPEND_AUTOMATIC_BUYING = "suspend_automatic_buying"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        prices = (self.open, self.high, self.low, self.close)
        if any(price <= 0 for price in prices):
            raise ValueError("OHLC prices must be greater than zero")
        if self.volume < 0:
            raise ValueError("volume must not be negative")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must be the greatest OHLC price")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must be the smallest OHLC price")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must include a timezone")


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    opened_at: datetime

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.average_entry_price <= 0:
            raise ValueError("position quantity and entry price must be positive")

    @property
    def cost_basis(self) -> Decimal:
        """Cash originally spent on the remaining quantity, including buy costs."""

        return self.quantity * self.average_entry_price


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    side: TradeSide
    timestamp: datetime
    quantity: Decimal
    market_price: Decimal
    simulated_price: Decimal
    fees: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    strategy_version: str
    reason: str

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("trade quantity must be positive")
        if self.market_price <= 0 or self.simulated_price <= 0:
            raise ValueError("trade prices must be positive")
        if any(cost < 0 for cost in (self.fees, self.spread_cost, self.slippage_cost)):
            raise ValueError("trade costs must not be negative")
        if not self.strategy_version.strip() or not self.reason.strip():
            raise ValueError("trade version and reason must not be empty")

    @property
    def total_costs(self) -> Decimal:
        return self.fees + self.spread_cost + self.slippage_cost


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    state: MarketState
    timestamp: datetime
    price: Decimal
    reason: str
    facts: Mapping[str, str] = field(default_factory=dict)
    cash_budget: Decimal | None = None
    profit_reinvestment: Decimal = Decimal("0")
    reentry_stage: int | None = None

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("decision price must be positive")
        if not self.reason.strip():
            raise ValueError("decision reason must not be empty")
        if self.cash_budget is not None:
            if self.action is not Action.BUY or self.cash_budget <= 0:
                raise ValueError("cash_budget must be positive and used only for BUY")
        if self.profit_reinvestment < 0:
            raise ValueError("profit_reinvestment must not be negative")
        if self.profit_reinvestment > (self.cash_budget or Decimal("0")):
            raise ValueError("profit_reinvestment cannot exceed cash_budget")
        if self.reentry_stage is not None:
            if self.action is not Action.BUY or self.reentry_stage < 1:
                raise ValueError("reentry_stage must be positive and used only for BUY")


@dataclass(frozen=True, slots=True)
class StrategyResult:
    strategy_version: str
    starting_capital: Decimal
    ending_capital: Decimal
    trades: tuple[Trade, ...]
    decisions: tuple[Decision, ...]
    metrics: Mapping[str, Decimal] = field(default_factory=dict)
