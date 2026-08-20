from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_simulator import (
    CsvMarketDataLoader,
    ExperimentCase,
    ExperimentError,
    OutOfSampleSplitter,
    ParameterExperiment,
    load_asset_profile,
)


PROJECT_ROOT = Path(__file__).parents[1]


def _profile():  # type: ignore[no-untyped-def]
    return load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")


def _data():  # type: ignore[no-untyped-def]
    profile = _profile()
    return CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "experiment_example.csv", profile.symbol
    ).load()


def test_fraction_split_is_chronological_and_non_overlapping() -> None:
    split = OutOfSampleSplitter.by_fraction(_data(), Decimal("0.50"))

    assert len(split.development) == 6
    assert len(split.out_of_sample) == 6
    assert split.development.end < split.out_of_sample.start
    assert split.split_timestamp == split.out_of_sample.start


def test_timestamp_split_assigns_cutoff_to_holdout() -> None:
    cutoff = datetime(2025, 7, 2, tzinfo=timezone.utc)
    split = OutOfSampleSplitter.by_timestamp(_data(), cutoff)

    assert len(split.development) == 6
    assert len(split.out_of_sample) == 6
    assert split.out_of_sample.start == cutoff


@pytest.mark.parametrize("fraction", [Decimal("0"), Decimal("1")])
def test_split_rejects_empty_periods(fraction: Decimal) -> None:
    with pytest.raises(ExperimentError):
        OutOfSampleSplitter.by_fraction(_data(), fraction)


def test_case_rejects_identity_and_unknown_overrides() -> None:
    with pytest.raises(ExperimentError, match="identity"):
        ExperimentCase("bad", "BTC-test", {"symbol": 1})
    with pytest.raises(ExperimentError, match="unknown"):
        ExperimentCase("bad", "BTC-test", {"not_a_parameter": 1})


def test_experiment_preserves_order_versions_and_base_profile() -> None:
    profile = _profile()
    original_rate = profile.momentum_trailing_exit_rate
    split = OutOfSampleSplitter.by_fraction(_data(), Decimal("0.50"))
    cases = (
        ExperimentCase(
            "five percent trail",
            "BTC-exp-trail-5",
            {"momentum_trailing_exit_rate": Decimal("0.05")},
        ),
        ExperimentCase(
            "ten percent trail",
            "BTC-exp-trail-10",
            {"momentum_trailing_exit_rate": Decimal("0.10")},
        ),
    )

    comparison = ParameterExperiment(profile).run(split, cases)

    assert [outcome.label for outcome in comparison.outcomes] == [
        "five percent trail",
        "ten percent trail",
    ]
    assert [outcome.strategy_version for outcome in comparison.outcomes] == [
        "BTC-exp-trail-5",
        "BTC-exp-trail-10",
    ]
    assert profile.strategy_version == "BTC-v1.0"
    assert profile.momentum_trailing_exit_rate == original_rate
    assert (
        comparison.outcomes[0].development_report.total_return_rate
        != comparison.outcomes[1].development_report.total_return_rate
    )
    assert (
        comparison.outcomes[0].out_of_sample_report.total_return_rate
        != comparison.outcomes[1].out_of_sample_report.total_return_rate
    )
    for outcome in comparison.outcomes:
        assert outcome.development_report.strategy_version == outcome.strategy_version
        assert outcome.out_of_sample_report.strategy_version == outcome.strategy_version
        assert outcome.development_report.leverage_used == 0
        assert outcome.out_of_sample_report.leverage_used == 0
        assert outcome.development_report.starting_capital == profile.initial_investment
        assert outcome.out_of_sample_report.starting_capital == profile.initial_investment
    assert "not universal optima" in comparison.caution
    assert not hasattr(comparison, "winner")


def test_experiment_requires_unique_manual_versions() -> None:
    split = OutOfSampleSplitter.by_fraction(_data(), Decimal("0.50"))
    cases = (
        ExperimentCase("first", "duplicate", {"normal_trailing_exit_rate": Decimal("0.05")}),
        ExperimentCase("second", "duplicate", {"normal_trailing_exit_rate": Decimal("0.06")}),
    )

    with pytest.raises(ExperimentError, match="unique strategy_version"):
        ParameterExperiment(_profile()).run(split, cases)
