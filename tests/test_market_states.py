from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_simulator import (
    CsvMarketDataLoader,
    MarketSnapshot,
    MarketState,
    MarketStateClassifier,
    load_asset_profile,
)


PROJECT_ROOT = Path(__file__).parents[1]
NOW = datetime(2025, 3, 1, tzinfo=timezone.utc)


def test_classifies_decline_stabilisation_recovery_and_momentum() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "market_states_example.csv", profile.symbol
    ).load()
    classifier = MarketStateClassifier(profile)
    history = []
    previous = MarketState.NORMAL
    states = []

    for snapshot in data:
        history.append(snapshot)
        assessment = classifier.classify(history, previous)
        states.append(assessment.state)
        previous = assessment.state

    assert states == [
        MarketState.NORMAL,
        MarketState.DECLINING,
        MarketState.DECLINING,
        MarketState.DECLINING,
        MarketState.DECLINING,
        MarketState.STABILISING,
        MarketState.RECOVERING,
        MarketState.RECOVERING,
        MarketState.EXPLOSIVE_MOMENTUM,
    ]


def test_classifies_uptrend_below_explosive_threshold() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    classifier = MarketStateClassifier(profile)
    history = [_snapshot("100", 0), _snapshot("104", 1)]

    assessment = classifier.classify(history, MarketState.NORMAL)

    assert assessment.state is MarketState.UPTREND
    assert assessment.transition == "normal -> uptrend"
    assert "uptrend threshold" in assessment.reason


def test_stabilisation_requires_a_prior_decline() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    classifier = MarketStateClassifier(profile)
    history = [_snapshot("100", 0), _snapshot("100.5", 1)]

    assessment = classifier.classify(history, MarketState.NORMAL)

    assert assessment.state is MarketState.NORMAL


def test_assessment_explains_measurements_used() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    classifier = MarketStateClassifier(profile)

    assessment = classifier.classify(
        [_snapshot("100", 0), _snapshot("98", 1)], MarketState.NORMAL
    )

    assert assessment.state is MarketState.DECLINING
    assert assessment.facts["lookback_return"] == "-0.02"
    assert assessment.facts["decline_threshold"] == "0.02"


def test_rejects_reordered_history() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")
    classifier = MarketStateClassifier(profile)

    with pytest.raises(ValueError, match="chronological"):
        classifier.classify(
            [_snapshot("100", 1), _snapshot("98", 0)], MarketState.NORMAL
        )


def _snapshot(close: str, hour: int) -> MarketSnapshot:
    price = Decimal(close)
    return MarketSnapshot(
        symbol="BTC-USD",
        timestamp=NOW + timedelta(hours=hour),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("100"),
    )
