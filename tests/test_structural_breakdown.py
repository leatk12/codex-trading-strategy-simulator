from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from trading_simulator import (
    Action,
    Backtest,
    CsvMarketDataLoader,
    MarketSnapshot,
    MarketState,
    Position,
    Decision,
    Strategy,
    StrategyContext,
    StructuralBreakdownPolicy,
    load_asset_profile,
)


PROJECT_ROOT = Path(__file__).parents[1]
NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _profile():  # type: ignore[no-untyped-def]
    return load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")


def _snapshots(*prices: str) -> list[MarketSnapshot]:
    snapshots = []
    for hour, text in enumerate(prices):
        price = Decimal(text)
        snapshots.append(
            MarketSnapshot(
                symbol="BTC-USD",
                timestamp=NOW + timedelta(hours=hour),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal("100"),
            )
        )
    return snapshots


def test_detects_large_decline_from_all_in_entry() -> None:
    profile = _profile()
    policy = StructuralBreakdownPolicy(profile)
    position = Position("BTC-USD", Decimal("10"), Decimal("100"), NOW)

    assessment = policy.assess(_snapshots("100", "60"), position)

    assert assessment.triggered
    assert assessment.facts["decline_from_entry"] == "0.4"
    assert any("all-in entry" in reason for reason in assessment.reasons)


def test_detects_range_break_without_using_an_entry_price() -> None:
    profile = _profile()
    policy = StructuralBreakdownPolicy(profile)

    assessment = policy.assess(
        _snapshots("100", "101", "99", "60"), position=None
    )

    assert assessment.triggered
    assert Decimal(assessment.facts["break_below_prior_range"]) > Decimal("0.20")
    assert any("prior closing range" in reason for reason in assessment.reasons)


def test_detects_extreme_rolling_volatility() -> None:
    profile = replace(
        _profile(),
        structural_breakdown_rate=Decimal("1"),
        structural_peak_drawdown_rate=Decimal("1"),
        structural_range_break_rate=Decimal("1"),
        extreme_volatility_rate=Decimal("0.05"),
        persistent_decline_candles=100,
    )
    assessment = StructuralBreakdownPolicy(profile).assess(
        _snapshots("100", "120", "100"), None
    )

    assert assessment.triggered
    assert any("volatility" in reason for reason in assessment.reasons)


def test_detects_persistent_decline() -> None:
    profile = replace(
        _profile(),
        structural_breakdown_rate=Decimal("1"),
        structural_peak_drawdown_rate=Decimal("1"),
        structural_range_break_rate=Decimal("1"),
        extreme_volatility_rate=Decimal("1"),
        persistent_decline_candles=3,
    )
    assessment = StructuralBreakdownPolicy(profile).assess(
        _snapshots("100", "99", "98", "97"), None
    )

    assert assessment.triggered
    assert assessment.facts["consecutive_declining_closes"] == "3"


def test_manual_review_latches_and_blocks_reentry_without_approval() -> None:
    profile = _profile()
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "structural_breakdown_example.csv",
        profile.symbol,
    ).load()

    result = Backtest(profile, data).run()

    assert [trade.side.value for trade in result.trades] == ["buy", "sell"]
    assert result.decisions[2].state is MarketState.STRUCTURAL_BREAKDOWN
    assert result.decisions[2].action is Action.SUSPEND_AUTOMATIC_BUYING
    assert result.decisions[3].state is MarketState.MANUAL_REVIEW
    assert result.decisions[-1].state is MarketState.MANUAL_REVIEW
    assert result.decisions[-1].action is Action.SUSPEND_AUTOMATIC_BUYING
    assert result.metrics["manual_review_events"] == Decimal("1")
    assert result.metrics["manual_approvals"] == Decimal("0")


def test_simulated_manual_approval_resumes_buying_and_can_retrigger_later() -> None:
    profile = _profile()
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "structural_breakdown_example.csv",
        profile.symbol,
    ).load()
    approval_at = datetime(2025, 6, 9, tzinfo=timezone.utc)

    result = Backtest(
        profile, data, manual_approval_at=approval_at
    ).run()

    assert [trade.side.value for trade in result.trades] == ["buy", "sell", "buy"]
    approval_decision = result.decisions[4]
    assert approval_decision.action is Action.BUY
    assert approval_decision.facts["manual_approval_on_candle"] == "true"
    assert result.decisions[-1].state is MarketState.STRUCTURAL_BREAKDOWN
    assert result.decisions[-1].action is Action.SUSPEND_AUTOMATIC_BUYING
    assert result.metrics["manual_review_events"] == Decimal("2")
    assert result.metrics["manual_approvals"] == Decimal("1")


def test_coordinator_blocks_buy_requested_by_unsafe_strategy() -> None:
    profile = _profile()
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "structural_breakdown_example.csv",
        profile.symbol,
    ).load()

    result = Backtest(profile, data, strategy=_AlwaysBuyStrategy(profile)).run()

    assert len(result.trades) == 2
    assert result.decisions[2].action is Action.SUSPEND_AUTOMATIC_BUYING
    assert result.decisions[2].facts["blocked_buy_budget"] == "1"


class _AlwaysBuyStrategy(Strategy):
    """Intentionally unsafe policy used to test the independent risk boundary."""

    def evaluate(
        self, snapshot: MarketSnapshot, context: StrategyContext
    ) -> Decision:
        return Decision(
            action=Action.BUY,
            state=context.state,
            timestamp=snapshot.timestamp,
            price=snapshot.close,
            reason="Unsafe test strategy always requests another purchase.",
            cash_budget=Decimal("1"),
        )
