from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_simulator import (
    Action,
    FixedProfitReentryStrategy,
    MarketSnapshot,
    MarketState,
    Portfolio,
    StrategyContext,
    load_asset_profile,
)


PROJECT_ROOT = Path(__file__).parents[1]
NOW = datetime(2025, 2, 1, tzinfo=timezone.utc)


def _profile():  # type: ignore[no-untyped-def]
    return load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")


def _snapshot(price: str) -> MarketSnapshot:
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


def _context(portfolio: Portfolio) -> StrategyContext:
    return StrategyContext(
        state=MarketState.NORMAL,
        cash=portfolio.cash,
        realised_profit=portfolio.realised_profit,
        base_capital=portfolio.base_capital,
        position=portfolio.position,
        previous_buy_price=portfolio.previous_buy_price,
        previous_sale_price=portfolio.previous_sale_price,
    )


def test_nominal_five_percent_rise_is_not_net_five_percent_profit() -> None:
    profile = _profile()
    portfolio = Portfolio(profile)
    portfolio.buy(Decimal("1000"), Decimal("100"), NOW, "Test entry")
    strategy = FixedProfitReentryStrategy(profile)

    decision = strategy.evaluate(_snapshot("105"), _context(portfolio))

    assert decision.action is Action.HOLD
    assert Decimal(decision.facts["net_profit_rate_if_sold"]) < Decimal("0.05")
    assert "selling costs" in decision.reason


def test_sells_when_net_profit_target_is_reached() -> None:
    profile = _profile()
    portfolio = Portfolio(profile)
    portfolio.buy(Decimal("1000"), Decimal("100"), NOW, "Test entry")
    strategy = FixedProfitReentryStrategy(profile)

    decision = strategy.evaluate(_snapshot("106"), _context(portfolio))

    assert decision.action is Action.SELL
    assert Decimal(decision.facts["net_profit_rate_if_sold"]) >= Decimal("0.05")


def test_waits_then_reenters_at_previous_buy_threshold() -> None:
    profile = _profile()
    strategy = FixedProfitReentryStrategy(profile)
    base_context = StrategyContext(
        state=MarketState.NORMAL,
        cash=Decimal("1050"),
        realised_profit=Decimal("50"),
        base_capital=Decimal("1000"),
        previous_buy_price=Decimal("100.15"),
        previous_sale_price=Decimal("105.84"),
    )

    waiting = strategy.evaluate(_snapshot("101"), base_context)
    reentry = strategy.evaluate(_snapshot("100"), base_context)

    assert waiting.action is Action.HOLD
    assert reentry.action is Action.BUY
    assert "realised-profit slice" in reentry.reason


def test_respects_automatic_buying_suspension() -> None:
    profile = _profile()
    strategy = FixedProfitReentryStrategy(profile)
    context = StrategyContext(
        state=MarketState.NORMAL,
        cash=Decimal("1000"),
        realised_profit=Decimal("0"),
        base_capital=Decimal("1000"),
        automatic_buying_enabled=False,
    )

    decision = strategy.evaluate(_snapshot("100"), context)

    assert decision.action is Action.SUSPEND_AUTOMATIC_BUYING
