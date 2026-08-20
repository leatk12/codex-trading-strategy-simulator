from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_simulator import Portfolio, PortfolioError, load_asset_profile


PROJECT_ROOT = Path(__file__).parents[1]
NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _portfolio(starting_cash: str = "2000") -> Portfolio:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    return Portfolio(profile, starting_cash=Decimal(starting_cash))


def test_buy_reduces_cash_and_creates_all_in_cost_basis() -> None:
    portfolio = _portfolio()

    trade = portfolio.buy(
        cash_budget=Decimal("1000"),
        market_price=Decimal("100"),
        timestamp=NOW,
        reason="Initial allocation",
    )

    assert portfolio.cash == Decimal("1000")
    assert portfolio.invested_capital == Decimal("1000")
    assert portfolio.position is not None
    assert portfolio.position.average_entry_price > trade.simulated_price
    assert portfolio.previous_buy_price == trade.simulated_price
    assert trade.strategy_version == "BTC-v1.0"


def test_profitable_round_trip_updates_realised_profit() -> None:
    portfolio = _portfolio()
    portfolio.buy(Decimal("1000"), Decimal("100"), NOW, "Initial allocation")

    sale = portfolio.sell_all(
        market_price=Decimal("120"),
        timestamp=NOW + timedelta(hours=1),
        reason="Manual test exit",
    )

    assert portfolio.position is None
    assert portfolio.realised_profit == portfolio.cash - portfolio.starting_capital
    assert portfolio.realised_profit > 0
    assert portfolio.previous_sale_price == sale.simulated_price
    assert len(portfolio.trades) == 2


def test_partial_sale_preserves_average_entry_price() -> None:
    portfolio = _portfolio()
    portfolio.buy(Decimal("1000"), Decimal("100"), NOW, "Initial allocation")
    assert portfolio.position is not None
    original_average = portfolio.position.average_entry_price
    half = portfolio.position.quantity / Decimal("2")

    portfolio.sell(
        half, Decimal("110"), NOW + timedelta(hours=1), "Partial reduction"
    )

    assert portfolio.position is not None
    assert portfolio.position.average_entry_price == original_average
    assert portfolio.invested_capital == Decimal("500")


def test_unrealised_profit_includes_expected_selling_costs() -> None:
    portfolio = _portfolio()
    portfolio.buy(Decimal("1000"), Decimal("100"), NOW, "Initial allocation")

    assert portfolio.unrealised_profit(Decimal("100")) < 0
    assert portfolio.liquidation_value(Decimal("100")) < portfolio.starting_capital


def test_cannot_spend_more_cash_than_available() -> None:
    portfolio = _portfolio("500")

    with pytest.raises(PortfolioError, match="available cash"):
        portfolio.buy(Decimal("501"), Decimal("100"), NOW, "Overspend")


def test_leverage_is_a_non_configurable_prohibited_invariant() -> None:
    portfolio = _portfolio("1000")

    assert portfolio.leverage_allowed is False
    assert portfolio.buying_power == portfolio.cash == Decimal("1000")
    assert portfolio.leverage_used == Decimal("0")
    with pytest.raises(PortfolioError, match="leverage is prohibited"):
        portfolio.buy(Decimal("1000.01"), Decimal("100"), NOW, "Borrowed buy")


def test_cannot_exceed_maximum_position_size_by_averaging_in() -> None:
    portfolio = _portfolio("4000")
    portfolio.buy(Decimal("2000"), Decimal("100"), NOW, "First allocation")

    with pytest.raises(PortfolioError, match="maximum_position_size"):
        portfolio.buy(
            Decimal("501"),
            Decimal("90"),
            NOW + timedelta(hours=1),
            "Second allocation",
        )


def test_cannot_sell_more_than_is_held() -> None:
    portfolio = _portfolio()
    portfolio.buy(Decimal("1000"), Decimal("100"), NOW, "Initial allocation")
    assert portfolio.position is not None

    with pytest.raises(PortfolioError, match="no more than held"):
        portfolio.sell(
            portfolio.position.quantity + Decimal("1"),
            Decimal("110"),
            NOW + timedelta(hours=1),
            "Invalid sale",
        )


def test_rejects_trade_timestamp_before_previous_trade() -> None:
    portfolio = _portfolio()
    portfolio.buy(
        Decimal("1000"),
        Decimal("100"),
        NOW + timedelta(hours=1),
        "Initial allocation",
    )

    with pytest.raises(PortfolioError, match="chronological"):
        portfolio.sell_all(Decimal("110"), NOW, "Time-travelling sale")
