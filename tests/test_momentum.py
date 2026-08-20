from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_simulator import (
    Action,
    Backtest,
    CsvMarketDataLoader,
    FixedProfitReentryStrategy,
    MarketSnapshot,
    MarketState,
    Portfolio,
    StrategyContext,
    TradeSide,
    load_asset_profile,
)


PROJECT_ROOT = Path(__file__).parents[1]
NOW = datetime(2025, 4, 1, tzinfo=timezone.utc)


def _profile():  # type: ignore[no-untyped-def]
    return load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")


def _invested_portfolio() -> Portfolio:
    portfolio = Portfolio(_profile())
    portfolio.buy(Decimal("1000"), Decimal("100"), NOW, "Test entry")
    return portfolio


def _snapshot(close: str) -> MarketSnapshot:
    price = Decimal(close)
    return MarketSnapshot(
        symbol="BTC-USD",
        timestamp=NOW,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("100"),
    )


def _momentum_context(
    portfolio: Portfolio, state: MarketState, peak: str
) -> StrategyContext:
    return StrategyContext(
        state=state,
        cash=portfolio.cash,
        realised_profit=portfolio.realised_profit,
        base_capital=portfolio.base_capital,
        position=portfolio.position,
        previous_buy_price=portfolio.previous_buy_price,
        momentum_active=True,
        momentum_peak_price=Decimal(peak),
        momentum_triggered_at=NOW,
    )


def test_momentum_holds_after_normal_profit_target_is_exceeded() -> None:
    portfolio = _invested_portfolio()
    strategy = FixedProfitReentryStrategy(portfolio.profile)
    context = _momentum_context(
        portfolio, MarketState.EXPLOSIVE_MOMENTUM, "113"
    )

    decision = strategy.evaluate(_snapshot("113"), context)

    assert Decimal(decision.facts["net_profit_rate_if_sold"]) > Decimal("0.05")
    assert decision.action is Action.HOLD
    assert decision.facts["trailing_exit_rate"] == "0.08"
    assert decision.facts["trailing_exit_threshold"] == "103.96"


def test_cooled_momentum_uses_normal_trailing_distance() -> None:
    portfolio = _invested_portfolio()
    strategy = FixedProfitReentryStrategy(portfolio.profile)
    context = _momentum_context(portfolio, MarketState.NORMAL, "126")

    decision = strategy.evaluate(_snapshot("120"), context)

    assert decision.action is Action.HOLD
    assert decision.facts["trailing_regime"] == "normal"
    assert decision.facts["trailing_exit_rate"] == "0.05"
    assert decision.facts["trailing_exit_threshold"] == "119.70"


def test_trailing_exit_sells_when_close_crosses_peak_threshold() -> None:
    portfolio = _invested_portfolio()
    strategy = FixedProfitReentryStrategy(portfolio.profile)
    context = _momentum_context(portfolio, MarketState.NORMAL, "126")

    decision = strategy.evaluate(_snapshot("119"), context)

    assert decision.action is Action.SELL
    assert "trailing exit triggered" in decision.reason


def test_backtest_tracks_peak_tightens_trail_and_resets_after_sale() -> None:
    profile = _profile()
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "momentum_strategy_example.csv", profile.symbol
    ).load()

    result = Backtest(profile, data).run()

    assert [decision.action for decision in result.decisions] == [
        Action.BUY,
        Action.HOLD,
        Action.HOLD,
        Action.HOLD,
        Action.SELL,
        Action.BUY,
    ]
    assert [trade.side for trade in result.trades] == [
        TradeSide.BUY,
        TradeSide.SELL,
        TradeSide.BUY,
    ]
    trigger = result.decisions[1]
    new_high = result.decisions[2]
    cooled = result.decisions[3]
    exit_decision = result.decisions[4]
    reentry = result.decisions[5]

    # Trigger candle high is 116, but the state is only known at its 113 close.
    assert trigger.facts["peak_since_momentum_trigger"] == "113"
    assert new_high.facts["peak_since_momentum_trigger"] == "126"
    assert cooled.facts["peak_since_momentum_trigger"] == "126"
    assert cooled.facts["trailing_exit_rate"] == "0.05"
    assert exit_decision.facts["trailing_exit_threshold"] == "119.70"
    assert "momentum_active" not in reentry.facts
