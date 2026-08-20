from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_simulator import (
    Action,
    Backtest,
    CsvMarketDataLoader,
    FixedProfitReentryStrategy,
    MarketSnapshot,
    MarketState,
    StrategyContext,
    load_asset_profile,
)


PROJECT_ROOT = Path(__file__).parents[1]
NOW = datetime(2025, 5, 1, tzinfo=timezone.utc)


def _profile():  # type: ignore[no-untyped-def]
    return load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")


def _snapshot(price: str, state: MarketState = MarketState.NORMAL) -> MarketSnapshot:
    value = Decimal(price)
    return MarketSnapshot(
        symbol="BTC-USD",
        timestamp=NOW,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("100"),
    )


def test_primary_reentry_adds_configured_profit_slice() -> None:
    profile = _profile()
    strategy = FixedProfitReentryStrategy(profile)
    context = StrategyContext(
        state=MarketState.NORMAL,
        cash=Decimal("1050"),
        realised_profit=Decimal("50"),
        base_capital=Decimal("1000"),
        previous_buy_price=Decimal("100.15"),
        reentry_profit_pool=Decimal("50"),
    )

    decision = strategy.evaluate(_snapshot("100"), context)

    assert decision.action is Action.BUY
    assert decision.cash_budget == Decimal("1012.50")
    assert decision.profit_reinvestment == Decimal("12.50")
    assert decision.reentry_stage == 1
    assert decision.facts["reentry_kind"] == "primary"


def test_conservative_reentry_above_previous_buy_uses_smaller_slice() -> None:
    profile = _profile()
    strategy = FixedProfitReentryStrategy(profile)
    context = StrategyContext(
        state=MarketState.STABILISING,
        cash=Decimal("1050"),
        realised_profit=Decimal("50"),
        base_capital=Decimal("1000"),
        previous_buy_price=Decimal("100.15"),
        reentry_profit_pool=Decimal("50"),
    )

    decision = strategy.evaluate(_snapshot("102"), context)

    assert decision.action is Action.BUY
    assert decision.cash_budget == Decimal("1005.00")
    assert decision.profit_reinvestment == Decimal("5.00")
    assert decision.facts["reentry_kind"] == "conservative"


def test_staged_backtest_waits_rejects_decline_then_buys_on_stabilisation() -> None:
    profile = _profile()
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "staged_reentry_example.csv", profile.symbol
    ).load()

    result = Backtest(profile, data).run()

    assert [decision.action for decision in result.decisions] == [
        Action.BUY,
        Action.SELL,
        Action.BUY,
        Action.HOLD,
        Action.HOLD,
        Action.HOLD,
        Action.HOLD,
        Action.HOLD,
        Action.HOLD,
        Action.HOLD,
        Action.HOLD,
        Action.BUY,
    ]
    first_reentry = result.decisions[2]
    declining_evaluation = result.decisions[6]
    additional_stage = result.decisions[-1]

    assert first_reentry.reentry_stage == 1
    assert declining_evaluation.state is MarketState.DECLINING
    assert "still declining" in declining_evaluation.reason
    assert additional_stage.state is MarketState.STABILISING
    assert additional_stage.reentry_stage == 2
    assert additional_stage.profit_reinvestment == pytest.approx(
        result.metrics["realised_profit"] * profile.staged_reentry_profit_rate
    )
    assert additional_stage.cash_budget == additional_stage.profit_reinvestment


def test_total_profit_reinvestment_never_exceeds_cycle_pool() -> None:
    profile = _profile()
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "staged_reentry_example.csv", profile.symbol
    ).load()

    result = Backtest(profile, data).run()
    reinvested = sum(
        (decision.profit_reinvestment for decision in result.decisions),
        Decimal("0"),
    )

    assert reinvested <= result.metrics["realised_profit"]
    assert reinvested == pytest.approx(
        result.metrics["realised_profit"] * Decimal("0.50")
    )
