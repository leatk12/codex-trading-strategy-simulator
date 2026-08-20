"""Portfolio state and auditable simulated transaction accounting."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .config import AssetProfile
from .domain import Position, Trade, TradeSide
from .execution import ExecutionQuote, TradingCostModel


class PortfolioError(ValueError):
    """Raised when an operation would violate a portfolio invariant."""


class Portfolio:
    """A cash-only account containing at most one long position in Version 1.

    Leverage is a prohibited system invariant, not a configurable feature.
    """

    leverage_allowed = False

    def __init__(
        self,
        profile: AssetProfile,
        starting_cash: Decimal | None = None,
        cost_model: TradingCostModel | None = None,
    ) -> None:
        cash = profile.initial_investment if starting_cash is None else starting_cash
        if cash <= 0 or not cash.is_finite():
            raise PortfolioError("starting_cash must be positive and finite")

        self.profile = profile
        self.cost_model = cost_model or TradingCostModel.from_profile(profile)
        self.starting_capital = cash
        self.base_capital = profile.initial_investment
        self.cash = cash
        self.realised_profit = Decimal("0")
        self.position: Position | None = None
        self.previous_buy_price: Decimal | None = None
        self.previous_sale_price: Decimal | None = None
        self.largest_position = Decimal("0")
        self._trades: list[Trade] = []
        self._last_trade_timestamp: datetime | None = None

    @property
    def trades(self) -> tuple[Trade, ...]:
        return tuple(self._trades)

    @property
    def invested_capital(self) -> Decimal:
        return Decimal("0") if self.position is None else self.position.cost_basis

    @property
    def buying_power(self) -> Decimal:
        """Cash available for purchases; borrowing never increases this value."""

        return self.cash

    @property
    def leverage_used(self) -> Decimal:
        return Decimal("0")

    @property
    def total_trading_costs(self) -> Decimal:
        return sum((trade.total_costs for trade in self._trades), Decimal("0"))

    def buy(
        self,
        cash_budget: Decimal,
        market_price: Decimal,
        timestamp: datetime,
        reason: str,
    ) -> Trade:
        """Buy using a cash budget that includes all entry transaction costs."""

        self._validate_trade_metadata(timestamp, reason)
        if cash_budget > self.cash:
            raise PortfolioError(
                "cash_budget exceeds available cash; leverage is prohibited"
            )
        quote = self.cost_model.quote_buy_for_budget(market_price, cash_budget)
        new_cost_basis = self.invested_capital + quote.cash_amount
        if new_cost_basis > self.profile.maximum_position_size:
            raise PortfolioError("purchase would exceed maximum_position_size")

        if self.position is not None and self.position.symbol != self.profile.symbol:
            raise PortfolioError("Version 1 portfolio supports only one asset")

        old_quantity = (
            Decimal("0") if self.position is None else self.position.quantity
        )
        opened_at = timestamp if self.position is None else self.position.opened_at
        new_quantity = old_quantity + quote.quantity
        self.position = Position(
            symbol=self.profile.symbol,
            quantity=new_quantity,
            average_entry_price=new_cost_basis / new_quantity,
            opened_at=opened_at,
        )
        self.cash -= quote.cash_amount
        self.previous_buy_price = quote.execution_price
        self.largest_position = max(self.largest_position, new_cost_basis)
        trade = self._record_trade(quote, timestamp, reason)
        self._check_invariants()
        return trade

    def sell(
        self,
        quantity: Decimal,
        market_price: Decimal,
        timestamp: datetime,
        reason: str,
    ) -> Trade:
        """Sell some or all of the current long position."""

        self._validate_trade_metadata(timestamp, reason)
        if self.position is None:
            raise PortfolioError("cannot sell without an open position")
        if quantity <= 0 or quantity > self.position.quantity:
            raise PortfolioError("sell quantity must be positive and no more than held")

        quote = self.cost_model.quote_sell(market_price, quantity)
        allocated_cost_basis = quantity * self.position.average_entry_price
        self.realised_profit += quote.cash_amount - allocated_cost_basis
        self.cash += quote.cash_amount
        remaining_quantity = self.position.quantity - quantity
        if remaining_quantity == 0:
            self.position = None
        else:
            self.position = Position(
                symbol=self.position.symbol,
                quantity=remaining_quantity,
                average_entry_price=self.position.average_entry_price,
                opened_at=self.position.opened_at,
            )
        self.previous_sale_price = quote.execution_price
        trade = self._record_trade(quote, timestamp, reason)
        self._check_invariants()
        return trade

    def sell_all(
        self, market_price: Decimal, timestamp: datetime, reason: str
    ) -> Trade:
        if self.position is None:
            raise PortfolioError("cannot sell without an open position")
        return self.sell(self.position.quantity, market_price, timestamp, reason)

    def liquidation_value(self, market_price: Decimal) -> Decimal:
        """Cash that would remain if the current position were sold now."""

        if self.position is None:
            return self.cash
        quote = self.cost_model.quote_sell(market_price, self.position.quantity)
        return self.cash + quote.cash_amount

    def unrealised_profit(self, market_price: Decimal) -> Decimal:
        """Profit/loss on the open position after expected exit costs."""

        if self.position is None:
            return Decimal("0")
        quote = self.cost_model.quote_sell(market_price, self.position.quantity)
        return quote.cash_amount - self.position.cost_basis

    def _record_trade(
        self, quote: ExecutionQuote, timestamp: datetime, reason: str
    ) -> Trade:
        trade = Trade(
            symbol=self.profile.symbol,
            side=quote.side,
            timestamp=timestamp,
            quantity=quote.quantity,
            market_price=quote.market_price,
            simulated_price=quote.execution_price,
            fees=quote.fee,
            spread_cost=quote.spread_cost,
            slippage_cost=quote.slippage_cost,
            strategy_version=self.profile.strategy_version,
            reason=reason,
        )
        self._trades.append(trade)
        self._last_trade_timestamp = timestamp
        return trade

    def _validate_trade_metadata(self, timestamp: datetime, reason: str) -> None:
        if timestamp.tzinfo is None:
            raise PortfolioError("trade timestamp must include a timezone")
        if (
            self._last_trade_timestamp is not None
            and timestamp < self._last_trade_timestamp
        ):
            raise PortfolioError("trade timestamps must be chronological")
        if not reason.strip():
            raise PortfolioError("trade reason must not be empty")

    def _check_invariants(self) -> None:
        if self.cash < 0:
            raise RuntimeError("portfolio invariant broken: cash became negative")
        if self.invested_capital > self.profile.maximum_position_size:
            raise RuntimeError("portfolio invariant broken: position is too large")
