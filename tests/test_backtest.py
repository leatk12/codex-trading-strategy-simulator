from decimal import Decimal
from pathlib import Path

import pytest

from trading_simulator import (
    Action,
    Backtest,
    CsvMarketDataLoader,
    TradeSide,
    load_asset_profile,
)


PROJECT_ROOT = Path(__file__).parents[1]


def test_basic_backtest_records_every_decision_and_trade() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "basic_strategy_example.csv",
        profile.symbol,
    ).load()

    result = Backtest(profile, data).run()

    assert [decision.action for decision in result.decisions] == [
        Action.BUY,
        Action.SELL,
        Action.HOLD,
        Action.BUY,
        Action.HOLD,
    ]
    assert [trade.side for trade in result.trades] == [
        TradeSide.BUY,
        TradeSide.SELL,
        TradeSide.BUY,
    ]
    assert all(trade.strategy_version == "BTC-v1.0" for trade in result.trades)
    assert len(result.decisions) == len(data)


def test_backtest_reinvests_only_configured_profit_slice_on_reentry() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "basic_strategy_example.csv",
        profile.symbol,
    ).load()

    result = Backtest(profile, data).run()
    second_purchase = result.trades[2]
    second_purchase_cash_cost = (
        second_purchase.quantity * second_purchase.simulated_price
        + second_purchase.fees
    )

    expected_budget = profile.initial_investment + (
        result.metrics["realised_profit"] * profile.staged_reentry_profit_rate
    )
    assert second_purchase_cash_cost == pytest.approx(expected_budget)
    assert result.metrics["ending_cash"] > Decimal("0")
    assert result.metrics["realised_profit"] > Decimal("0")


def test_ending_capital_includes_net_liquidation_value() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "basic_strategy_example.csv",
        profile.symbol,
    ).load()

    result = Backtest(profile, data).run()

    reentry = result.trades[2]
    expected_cost_basis = (
        reentry.quantity * reentry.simulated_price + reentry.fees
    )
    assert result.metrics["invested_capital"] == expected_cost_basis
    assert result.metrics["unrealised_profit"] > Decimal("0")
    assert result.ending_capital > result.starting_capital
    assert result.metrics["total_return_rate"] == (
        result.ending_capital - result.starting_capital
    ) / result.starting_capital


def test_backtest_decisions_include_explainable_state_transitions() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "market_states_example.csv",
        profile.symbol,
    ).load()

    result = Backtest(profile, data).run()

    assert result.decisions[1].state.value == "declining"
    assert (
        result.decisions[1].facts["market_state_transition"]
        == "normal -> declining"
    )
    assert "market_state_reason" in result.decisions[1].facts
    assert result.decisions[-1].state.value == "explosive_momentum"
