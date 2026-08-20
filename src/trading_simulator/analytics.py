"""Performance analytics and cost-matched buy-and-hold benchmarking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .config import AssetProfile
from .domain import StrategyResult, Trade, TradeSide
from .execution import TradingCostModel
from .market_data import HistoricalMarketData


class AnalyticsError(ValueError):
    """Raised when a result cannot be reconciled without invalid accounting."""


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    value: Decimal
    invested: bool


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    strategy_version: str
    starting_capital: Decimal
    ending_capital: Decimal
    total_return_rate: Decimal
    realised_profit: Decimal
    unrealised_profit: Decimal
    trade_count: int
    completed_trade_count: int
    profitable_trades: int
    losing_trades: int
    break_even_trades: int
    win_rate: Decimal
    average_profit_per_completed_trade: Decimal
    average_winning_trade: Decimal
    average_loss: Decimal
    maximum_drawdown: Decimal
    fees_paid: Decimal
    spread_cost_paid: Decimal
    slippage_cost_paid: Decimal
    total_trading_costs: Decimal
    invested_duration: timedelta
    cash_duration: timedelta
    invested_time_rate: Decimal
    cash_time_rate: Decimal
    largest_position: Decimal
    manual_review_events: int
    buy_and_hold_ending_capital: Decimal
    buy_and_hold_return_rate: Decimal
    excess_return_vs_buy_and_hold: Decimal
    period_return_volatility: Decimal
    period_sharpe_ratio: Decimal | None
    leverage_used: Decimal
    equity_curve: tuple[EquityPoint, ...]


class PerformanceAnalyzer:
    """Reconcile trade history into statistics without changing the strategy."""

    def __init__(self, profile: AssetProfile) -> None:
        self.profile = profile
        self.cost_model = TradingCostModel.from_profile(profile)

    def analyze(
        self, result: StrategyResult, data: HistoricalMarketData
    ) -> PerformanceReport:
        if data.symbol != self.profile.symbol:
            raise AnalyticsError("market data does not match the asset profile")
        trade_times = {snapshot.timestamp for snapshot in data}
        if any(trade.timestamp not in trade_times for trade in result.trades):
            raise AnalyticsError("every trade must correspond to a market snapshot")

        trades_by_time: dict[datetime, list[Trade]] = {}
        for trade in result.trades:
            trades_by_time.setdefault(trade.timestamp, []).append(trade)

        cash = result.starting_capital
        quantity = Decimal("0")
        cost_basis = Decimal("0")
        largest_position = Decimal("0")
        completed_profits: list[Decimal] = []
        equity_points: list[EquityPoint] = []
        invested_duration = timedelta(0)
        cash_duration = timedelta(0)

        for index, snapshot in enumerate(data):
            for trade in trades_by_time.get(snapshot.timestamp, []):
                if trade.side is TradeSide.BUY:
                    cash_cost = (
                        trade.quantity * trade.simulated_price + trade.fees
                    )
                    if cash_cost > cash:
                        raise AnalyticsError(
                            "trade history requires borrowed cash; leverage is prohibited"
                        )
                    cash -= cash_cost
                    quantity += trade.quantity
                    cost_basis += cash_cost
                    largest_position = max(largest_position, cost_basis)
                else:
                    if quantity <= 0 or trade.quantity > quantity:
                        raise AnalyticsError("trade history sells more than it holds")
                    allocated_basis = cost_basis * (trade.quantity / quantity)
                    net_proceeds = (
                        trade.quantity * trade.simulated_price - trade.fees
                    )
                    cash += net_proceeds
                    completed_profits.append(net_proceeds - allocated_basis)
                    quantity -= trade.quantity
                    cost_basis -= allocated_basis
                    if quantity == 0:
                        cost_basis = Decimal("0")

            if cash < 0:
                raise AnalyticsError("negative cash detected; leverage is prohibited")
            liquidation_proceeds = Decimal("0")
            if quantity > 0:
                liquidation_proceeds = self.cost_model.quote_sell(
                    snapshot.close, quantity
                ).cash_amount
            equity_points.append(
                EquityPoint(snapshot.timestamp, cash + liquidation_proceeds, quantity > 0)
            )

            if index < len(data) - 1:
                duration = data[index + 1].timestamp - snapshot.timestamp
                if quantity > 0:
                    invested_duration += duration
                else:
                    cash_duration += duration

        ending_capital = equity_points[-1].value
        tolerance = Decimal("1e-18")
        if abs(ending_capital - result.ending_capital) > tolerance:
            raise AnalyticsError("replayed ending capital does not match strategy result")

        unrealised_profit = (
            Decimal("0")
            if quantity == 0
            else equity_points[-1].value - cash - cost_basis
        )
        realised_profit = sum(completed_profits, Decimal("0"))
        profitable = [profit for profit in completed_profits if profit > 0]
        losing = [profit for profit in completed_profits if profit < 0]
        break_even = [profit for profit in completed_profits if profit == 0]
        completed_count = len(completed_profits)

        total_duration = invested_duration + cash_duration
        if total_duration.total_seconds() == 0:
            invested_rate = Decimal("0")
            cash_rate = Decimal("0")
        else:
            total_seconds = Decimal(str(total_duration.total_seconds()))
            invested_rate = Decimal(str(invested_duration.total_seconds())) / total_seconds
            cash_rate = Decimal(str(cash_duration.total_seconds())) / total_seconds

        total_return = (
            ending_capital - result.starting_capital
        ) / result.starting_capital
        buy_hold_ending = self._buy_and_hold_ending_capital(
            result.starting_capital, data
        )
        buy_hold_return = (
            buy_hold_ending - result.starting_capital
        ) / result.starting_capital
        period_returns = self._period_returns(
            equity_points, result.starting_capital
        )
        return_volatility = self._population_standard_deviation(period_returns)
        mean_return = (
            Decimal("0")
            if not period_returns
            else sum(period_returns, Decimal("0")) / Decimal(len(period_returns))
        )
        sharpe = (
            None if return_volatility == 0 else mean_return / return_volatility
        )

        fees = sum((trade.fees for trade in result.trades), Decimal("0"))
        spread = sum((trade.spread_cost for trade in result.trades), Decimal("0"))
        slippage = sum(
            (trade.slippage_cost for trade in result.trades), Decimal("0")
        )
        return PerformanceReport(
            strategy_version=result.strategy_version,
            starting_capital=result.starting_capital,
            ending_capital=ending_capital,
            total_return_rate=total_return,
            realised_profit=realised_profit,
            unrealised_profit=unrealised_profit,
            trade_count=len(result.trades),
            completed_trade_count=completed_count,
            profitable_trades=len(profitable),
            losing_trades=len(losing),
            break_even_trades=len(break_even),
            win_rate=(
                Decimal("0")
                if completed_count == 0
                else Decimal(len(profitable)) / Decimal(completed_count)
            ),
            average_profit_per_completed_trade=self._average(completed_profits),
            average_winning_trade=self._average(profitable),
            average_loss=self._average(losing),
            maximum_drawdown=self._maximum_drawdown(
                equity_points, result.starting_capital
            ),
            fees_paid=fees,
            spread_cost_paid=spread,
            slippage_cost_paid=slippage,
            total_trading_costs=fees + spread + slippage,
            invested_duration=invested_duration,
            cash_duration=cash_duration,
            invested_time_rate=invested_rate,
            cash_time_rate=cash_rate,
            largest_position=largest_position,
            manual_review_events=int(
                result.metrics.get("manual_review_events", Decimal("0"))
            ),
            buy_and_hold_ending_capital=buy_hold_ending,
            buy_and_hold_return_rate=buy_hold_return,
            excess_return_vs_buy_and_hold=total_return - buy_hold_return,
            period_return_volatility=return_volatility,
            period_sharpe_ratio=sharpe,
            leverage_used=Decimal("0"),
            equity_curve=tuple(equity_points),
        )

    def _buy_and_hold_ending_capital(
        self, starting_capital: Decimal, data: HistoricalMarketData
    ) -> Decimal:
        buy = self.cost_model.quote_buy_for_budget(data[0].close, starting_capital)
        sell = self.cost_model.quote_sell(data[-1].close, buy.quantity)
        return sell.cash_amount

    @staticmethod
    def _period_returns(
        points: list[EquityPoint], starting_capital: Decimal
    ) -> list[Decimal]:
        values = [starting_capital] + [point.value for point in points]
        return [
            (current - previous) / previous
            for previous, current in zip(values, values[1:])
        ]

    @staticmethod
    def _population_standard_deviation(values: list[Decimal]) -> Decimal:
        if not values:
            return Decimal("0")
        mean = sum(values, Decimal("0")) / Decimal(len(values))
        variance = sum(
            ((value - mean) ** 2 for value in values), Decimal("0")
        ) / Decimal(len(values))
        return variance.sqrt()

    @staticmethod
    def _maximum_drawdown(
        points: list[EquityPoint], starting_capital: Decimal
    ) -> Decimal:
        peak = starting_capital
        maximum = Decimal("0")
        for point in points:
            peak = max(peak, point.value)
            drawdown = (peak - point.value) / peak
            maximum = max(maximum, drawdown)
        return maximum

    @staticmethod
    def _average(values: list[Decimal]) -> Decimal:
        if not values:
            return Decimal("0")
        return sum(values, Decimal("0")) / Decimal(len(values))
