from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_simulator import MarketSnapshot, Trade, TradeSide


NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_market_snapshot_accepts_a_valid_candle() -> None:
    snapshot = MarketSnapshot(
        symbol="BTC-USD",
        timestamp=NOW,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("95"),
        close=Decimal("105"),
        volume=Decimal("42"),
    )

    assert snapshot.close == Decimal("105")


def test_market_snapshot_rejects_impossible_high() -> None:
    with pytest.raises(ValueError, match="high"):
        MarketSnapshot(
            symbol="BTC-USD",
            timestamp=NOW,
            open=Decimal("100"),
            high=Decimal("99"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("42"),
        )


def test_trade_preserves_strategy_version_and_sums_costs() -> None:
    trade = Trade(
        symbol="BTC-USD",
        side=TradeSide.BUY,
        timestamp=NOW,
        quantity=Decimal("0.1"),
        market_price=Decimal("100"),
        simulated_price=Decimal("100.20"),
        fees=Decimal("0.10"),
        spread_cost=Decimal("0.05"),
        slippage_cost=Decimal("0.02"),
        strategy_version="BTC-v1.0",
        reason="Initial allocation",
    )

    assert trade.total_costs == Decimal("0.17")
    assert trade.strategy_version == "BTC-v1.0"
