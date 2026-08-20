from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_simulator import (
    AnalyticsError,
    Backtest,
    CsvMarketDataLoader,
    HistoricalMarketData,
    MarketSnapshot,
    PerformanceAnalyzer,
    Portfolio,
    StrategyResult,
    Trade,
    TradeSide,
    load_asset_profile,
)


PROJECT_ROOT = Path(__file__).parents[1]
NOW = datetime(2025, 7, 1, tzinfo=timezone.utc)


def _profile():  # type: ignore[no-untyped-def]
    return load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")


def test_performance_report_covers_strategy_and_buy_hold() -> None:
    profile = _profile()
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "basic_strategy_example.csv", profile.symbol
    ).load()
    result = Backtest(profile, data).run()

    report = PerformanceAnalyzer(profile).analyze(result, data)

    assert report.trade_count == 3
    assert report.completed_trade_count == 1
    assert report.profitable_trades == 1
    assert report.losing_trades == 0
    assert report.win_rate == Decimal("1")
    assert report.average_profit_per_completed_trade > 0
    assert report.realised_profit == result.metrics["realised_profit"]
    assert report.unrealised_profit == result.metrics["unrealised_profit"]
    assert report.ending_capital == result.ending_capital
    assert report.maximum_drawdown > 0
    assert report.total_trading_costs == result.metrics["trading_costs"]
    assert report.invested_duration == timedelta(hours=2)
    assert report.cash_duration == timedelta(hours=2)
    assert report.invested_time_rate == Decimal("0.5")
    assert report.cash_time_rate == Decimal("0.5")
    assert report.excess_return_vs_buy_and_hold == (
        report.total_return_rate - report.buy_and_hold_return_rate
    )
    assert report.leverage_used == 0


def test_report_calculates_losing_completed_trade() -> None:
    profile = _profile()
    data = HistoricalMarketData((_snapshot("100", 0), _snapshot("90", 1)))
    portfolio = Portfolio(profile)
    portfolio.buy(Decimal("1000"), Decimal("100"), NOW, "Test buy")
    portfolio.sell_all(
        Decimal("90"), NOW + timedelta(hours=1), "Test losing sale"
    )
    result = StrategyResult(
        strategy_version=profile.strategy_version,
        starting_capital=portfolio.starting_capital,
        ending_capital=portfolio.cash,
        trades=portfolio.trades,
        decisions=(),
    )

    report = PerformanceAnalyzer(profile).analyze(result, data)

    assert report.completed_trade_count == 1
    assert report.profitable_trades == 0
    assert report.losing_trades == 1
    assert report.win_rate == 0
    assert report.average_loss < 0
    assert report.average_profit_per_completed_trade == report.average_loss
    assert report.maximum_drawdown > Decimal("0.10")


def test_report_includes_manual_review_events() -> None:
    profile = _profile()
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "structural_breakdown_example.csv", profile.symbol
    ).load()
    result = Backtest(profile, data).run()

    report = PerformanceAnalyzer(profile).analyze(result, data)

    assert report.manual_review_events == 1


def test_analytics_rejects_ledger_that_requires_leverage() -> None:
    profile = _profile()
    data = HistoricalMarketData((_snapshot("100", 0), _snapshot("101", 1)))
    impossible_buy = Trade(
        symbol=profile.symbol,
        side=TradeSide.BUY,
        timestamp=NOW,
        quantity=Decimal("11"),
        market_price=Decimal("100"),
        simulated_price=Decimal("100"),
        fees=Decimal("0"),
        spread_cost=Decimal("0"),
        slippage_cost=Decimal("0"),
        strategy_version=profile.strategy_version,
        reason="Deliberately impossible leveraged ledger",
    )
    result = StrategyResult(
        strategy_version=profile.strategy_version,
        starting_capital=Decimal("1000"),
        ending_capital=Decimal("1000"),
        trades=(impossible_buy,),
        decisions=(),
    )

    with pytest.raises(AnalyticsError, match="leverage is prohibited"):
        PerformanceAnalyzer(profile).analyze(result, data)


def _snapshot(price: str, hour: int) -> MarketSnapshot:
    value = Decimal(price)
    return MarketSnapshot(
        symbol="BTC-USD",
        timestamp=NOW + timedelta(hours=hour),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("100"),
    )
