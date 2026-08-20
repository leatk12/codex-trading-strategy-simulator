from decimal import Decimal

import pytest

from trading_simulator import ExecutionError, TradingCostModel


def test_buy_budget_includes_spread_slippage_and_fee() -> None:
    model = TradingCostModel(
        fee_rate=Decimal("0.01"),
        spread_rate=Decimal("0.02"),
        slippage_rate=Decimal("0.01"),
    )

    quote = model.quote_buy_for_budget(
        market_price=Decimal("100"), cash_budget=Decimal("103.02")
    )

    assert quote.execution_price == Decimal("102.00")
    assert quote.quantity == Decimal("1")
    assert quote.gross_notional == Decimal("102.00")
    assert quote.fee == Decimal("1.0200")
    assert quote.spread_cost == Decimal("1.00")
    assert quote.slippage_cost == Decimal("1.00")
    assert quote.cash_amount == Decimal("103.0200")


def test_fee_adjusted_break_even_recovers_all_entry_costs() -> None:
    model = TradingCostModel(
        fee_rate=Decimal("0.01"),
        spread_rate=Decimal("0.02"),
        slippage_rate=Decimal("0.01"),
    )
    buy = model.quote_buy_for_budget(Decimal("100"), Decimal("103.02"))

    break_even = model.break_even_market_price(
        buy.cash_amount / buy.quantity
    )
    sell = model.quote_sell(break_even, buy.quantity)

    assert sell.cash_amount == pytest.approx(buy.cash_amount)
    assert break_even > buy.market_price


def test_sell_quote_deducts_exit_costs() -> None:
    model = TradingCostModel(
        fee_rate=Decimal("0.01"),
        spread_rate=Decimal("0.02"),
        slippage_rate=Decimal("0.01"),
    )

    quote = model.quote_sell(Decimal("100"), Decimal("1"))

    assert quote.execution_price == Decimal("98.00")
    assert quote.gross_notional == Decimal("98.00")
    assert quote.fee == Decimal("0.9800")
    assert quote.cash_amount == Decimal("97.0200")


def test_rejects_costs_that_leave_no_sell_price() -> None:
    with pytest.raises(ExecutionError, match="no positive sell price"):
        TradingCostModel(
            fee_rate=Decimal("0.01"),
            spread_rate=Decimal("1"),
            slippage_rate=Decimal("0.5"),
        )

