"""Contracts that keep deterministic strategies separate from I/O."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .config import AssetProfile
from .domain import Decision, MarketSnapshot, MarketState, Position


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """All mutable backtest knowledge presented as an explicit value."""

    state: MarketState
    cash: Decimal
    realised_profit: Decimal
    base_capital: Decimal
    position: Position | None = None
    previous_buy_price: Decimal | None = None
    previous_sale_price: Decimal | None = None
    automatic_buying_enabled: bool = True
    momentum_active: bool = False
    momentum_peak_price: Decimal | None = None
    momentum_triggered_at: datetime | None = None
    reentry_stage: int = 0
    reentry_profit_pool: Decimal = Decimal("0")
    profit_reinvested: Decimal = Decimal("0")
    next_reentry_evaluation_at: datetime | None = None


@dataclass(slots=True)
class StrategyRuntimeState:
    """Mutable state owned by one backtest run, never by the strategy policy."""

    momentum_active: bool = False
    momentum_peak_price: Decimal | None = None
    momentum_triggered_at: datetime | None = None
    reentry_stage: int = 0
    reentry_profit_pool: Decimal = Decimal("0")
    profit_reinvested: Decimal = Decimal("0")
    next_reentry_evaluation_at: datetime | None = None
    manual_review_active: bool = False
    manual_review_triggered_at: datetime | None = None
    manual_review_reasons: tuple[str, ...] = ()
    manual_review_events: int = 0
    manual_approvals: int = 0

    def activate_momentum(self, price: Decimal, timestamp: datetime) -> None:
        if price <= 0:
            raise ValueError("momentum trigger price must be positive")
        self.momentum_active = True
        self.momentum_peak_price = price
        self.momentum_triggered_at = timestamp

    def update_momentum_peak(self, price: Decimal) -> None:
        if not self.momentum_active or self.momentum_peak_price is None:
            raise RuntimeError("cannot update a momentum peak before activation")
        self.momentum_peak_price = max(self.momentum_peak_price, price)

    def reset_momentum(self) -> None:
        self.momentum_active = False
        self.momentum_peak_price = None
        self.momentum_triggered_at = None

    def begin_reentry_cycle(self, realised_profit: Decimal) -> None:
        """Make positive realised profit eligible for configured staged slices."""

        self.reentry_stage = 0
        self.reentry_profit_pool = max(realised_profit, Decimal("0"))
        self.profit_reinvested = Decimal("0")
        self.next_reentry_evaluation_at = None

    def record_reentry(
        self,
        stage: int,
        profit_reinvestment: Decimal,
        next_evaluation_at: datetime,
    ) -> None:
        self.reentry_stage = stage
        self.profit_reinvested += profit_reinvestment
        if self.profit_reinvested > self.reentry_profit_pool:
            raise RuntimeError("reinvested profit exceeded the re-entry profit pool")
        self.next_reentry_evaluation_at = next_evaluation_at

    def postpone_reentry_evaluation(self, next_evaluation_at: datetime) -> None:
        self.next_reentry_evaluation_at = next_evaluation_at

    def activate_manual_review(
        self, timestamp: datetime, reasons: tuple[str, ...]
    ) -> None:
        if self.manual_review_active:
            return
        self.manual_review_active = True
        self.manual_review_triggered_at = timestamp
        self.manual_review_reasons = reasons
        self.manual_review_events += 1

    def approve_manual_review(self) -> None:
        if not self.manual_review_active:
            raise RuntimeError("manual review is not active")
        self.manual_review_active = False
        self.manual_review_triggered_at = None
        self.manual_review_reasons = ()
        self.manual_approvals += 1


class Strategy(ABC):
    """A deterministic decision rule with no data-download or execution duties."""

    def __init__(self, profile: AssetProfile) -> None:
        self.profile = profile

    @abstractmethod
    def evaluate(
        self, snapshot: MarketSnapshot, context: StrategyContext
    ) -> Decision:
        """Return one explainable action for the supplied point in time."""
