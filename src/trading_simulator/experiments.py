"""Manual parameter comparisons and independent out-of-sample evaluation."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from .analytics import PerformanceAnalyzer, PerformanceReport
from .backtest import Backtest
from .config import AssetProfile
from .market_data import HistoricalMarketData


class ExperimentError(ValueError):
    """Raised when an experiment could mutate or ambiguously identify rules."""


ExperimentValue = Decimal | int


@dataclass(frozen=True, slots=True)
class ExperimentCase:
    """One manually named and versioned set of profile overrides."""

    label: str
    strategy_version: str
    overrides: Mapping[str, ExperimentValue]

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ExperimentError("experiment label must not be empty")
        if not self.strategy_version.strip():
            raise ExperimentError("experiment strategy_version must not be empty")
        copied = dict(self.overrides)
        forbidden = {"symbol", "strategy_version"} & copied.keys()
        if forbidden:
            raise ExperimentError(
                f"experiment cannot override identity fields: {sorted(forbidden)}"
            )
        valid_fields = {field.name for field in fields(AssetProfile)}
        unknown = copied.keys() - valid_fields
        if unknown:
            raise ExperimentError(f"unknown profile overrides: {sorted(unknown)}")
        object.__setattr__(self, "overrides", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class DatasetSplit:
    development: HistoricalMarketData
    out_of_sample: HistoricalMarketData
    split_timestamp: datetime

    def __post_init__(self) -> None:
        if self.development.symbol != self.out_of_sample.symbol:
            raise ExperimentError("split periods must contain the same asset")
        if self.development.end >= self.out_of_sample.start:
            raise ExperimentError("development and out-of-sample periods overlap")


class OutOfSampleSplitter:
    """Create chronological, non-overlapping development and holdout periods."""

    @staticmethod
    def by_timestamp(
        data: HistoricalMarketData, split_timestamp: datetime
    ) -> DatasetSplit:
        if split_timestamp.tzinfo is None:
            raise ExperimentError("split_timestamp must include a timezone")
        development = tuple(
            snapshot for snapshot in data if snapshot.timestamp < split_timestamp
        )
        out_of_sample = tuple(
            snapshot for snapshot in data if snapshot.timestamp >= split_timestamp
        )
        if not development or not out_of_sample:
            raise ExperimentError("both split periods must contain data")
        return DatasetSplit(
            HistoricalMarketData(development),
            HistoricalMarketData(out_of_sample),
            out_of_sample[0].timestamp,
        )

    @staticmethod
    def by_fraction(
        data: HistoricalMarketData, development_fraction: Decimal
    ) -> DatasetSplit:
        if not Decimal("0") < development_fraction < Decimal("1"):
            raise ExperimentError("development_fraction must be between 0 and 1")
        split_index = int(Decimal(len(data)) * development_fraction)
        if split_index < 1 or split_index >= len(data):
            raise ExperimentError("split leaves one period empty")
        return DatasetSplit(
            HistoricalMarketData(data[:split_index]),
            HistoricalMarketData(data[split_index:]),
            data[split_index].timestamp,
        )


@dataclass(frozen=True, slots=True)
class ExperimentOutcome:
    label: str
    strategy_version: str
    overrides: Mapping[str, ExperimentValue]
    development_report: PerformanceReport
    out_of_sample_report: PerformanceReport


@dataclass(frozen=True, slots=True)
class ExperimentComparison:
    split: DatasetSplit
    outcomes: tuple[ExperimentOutcome, ...]
    caution: str = (
        "Historical comparisons are not universal optima. Do not select on the "
        "out-of-sample period and then continue calling that period unseen."
    )


class ParameterExperiment:
    """Run supplied cases without ranking, selecting, or modifying profiles."""

    def __init__(self, base_profile: AssetProfile) -> None:
        self.base_profile = base_profile

    def run(
        self,
        split: DatasetSplit,
        cases: tuple[ExperimentCase, ...],
    ) -> ExperimentComparison:
        if not cases:
            raise ExperimentError("at least one experiment case is required")
        if split.development.symbol != self.base_profile.symbol:
            raise ExperimentError("split data does not match the base profile")
        versions = [case.strategy_version for case in cases]
        if len(set(versions)) != len(versions):
            raise ExperimentError(
                "each experiment case must have a unique strategy_version"
            )

        outcomes = tuple(self._run_case(split, case) for case in cases)
        return ExperimentComparison(split, outcomes)

    def _run_case(
        self, split: DatasetSplit, case: ExperimentCase
    ) -> ExperimentOutcome:
        try:
            profile = replace(
                self.base_profile,
                strategy_version=case.strategy_version,
                **case.overrides,
            )
        except (TypeError, ValueError) as error:
            raise ExperimentError(
                f"invalid overrides for case {case.label!r}: {error}"
            ) from error

        development_result = Backtest(profile, split.development).run()
        out_of_sample_result = Backtest(profile, split.out_of_sample).run()
        analyzer = PerformanceAnalyzer(profile)
        return ExperimentOutcome(
            label=case.label,
            strategy_version=case.strategy_version,
            overrides=case.overrides,
            development_report=analyzer.analyze(
                development_result, split.development
            ),
            out_of_sample_report=analyzer.analyze(
                out_of_sample_result, split.out_of_sample
            ),
        )
