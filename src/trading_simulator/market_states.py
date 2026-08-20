"""Deterministic classification of recent price behaviour into market states."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Mapping, Sequence

from .config import AssetProfile
from .domain import MarketSnapshot, MarketState


@dataclass(frozen=True, slots=True)
class MarketStateAssessment:
    """One explainable state classification at one point in time."""

    state: MarketState
    previous_state: MarketState
    reason: str
    facts: Mapping[str, str] = field(default_factory=dict)

    @property
    def transition(self) -> str:
        return f"{self.previous_state.value} -> {self.state.value}"


class MarketStateClassifier:
    """Classify current behaviour using only current and earlier snapshots."""

    _RECOVERY_PREDECESSORS = {
        MarketState.DECLINING,
        MarketState.STABILISING,
        MarketState.RECOVERING,
    }

    def __init__(self, profile: AssetProfile) -> None:
        self.profile = profile

    def classify(
        self,
        history: Sequence[MarketSnapshot],
        previous_state: MarketState,
    ) -> MarketStateAssessment:
        if not history:
            raise ValueError("market-state classification requires price history")
        self._validate_history(history)
        current = history[-1]

        rapid_window = self._window(
            history, self.profile.rapid_appreciation_window_hours
        )
        rapid_return = self._return_rate(rapid_window)
        if (
            rapid_return is not None
            and rapid_return >= self.profile.rapid_appreciation_rate
        ):
            return self._assessment(
                MarketState.EXPLOSIVE_MOMENTUM,
                previous_state,
                "Price appreciation reached the rapid-movement threshold.",
                rapid_return=rapid_return,
                window=rapid_window,
            )

        state_window = self._window(
            history, self.profile.market_state_lookback_hours
        )
        lookback_return = self._return_rate(state_window)
        facts = self._base_facts(state_window, lookback_return, rapid_return)
        if lookback_return is None:
            return MarketStateAssessment(
                MarketState.NORMAL,
                previous_state,
                "At least two observations are required to classify a trend.",
                facts,
            )

        if lookback_return <= -self.profile.declining_rate:
            return MarketStateAssessment(
                MarketState.DECLINING,
                previous_state,
                "Lookback return reached the configured decline threshold.",
                facts,
            )

        closes = [snapshot.close for snapshot in state_window]
        recent_low = min(closes)
        recovery_from_low = (current.close - recent_low) / recent_low
        facts["recovery_from_recent_low"] = str(recovery_from_low)
        if (
            previous_state in self._RECOVERY_PREDECESSORS
            and recovery_from_low >= self.profile.recovery_rate
        ):
            return MarketStateAssessment(
                MarketState.RECOVERING,
                previous_state,
                "Price recovered sufficiently from its recent closing low.",
                facts,
            )

        closing_range_rate = (max(closes) - min(closes)) / min(closes)
        facts["closing_range_rate"] = str(closing_range_rate)
        if (
            previous_state
            in {MarketState.DECLINING, MarketState.STABILISING}
            and closing_range_rate <= self.profile.stabilising_range_rate
        ):
            return MarketStateAssessment(
                MarketState.STABILISING,
                previous_state,
                "Recent closing prices remained inside the configured narrow range.",
                facts,
            )

        if lookback_return >= self.profile.uptrend_rate:
            return MarketStateAssessment(
                MarketState.UPTREND,
                previous_state,
                "Lookback return reached the configured uptrend threshold.",
                facts,
            )

        return MarketStateAssessment(
            MarketState.NORMAL,
            previous_state,
            "No configured directional or stabilisation threshold was reached.",
            facts,
        )

    def _validate_history(self, history: Sequence[MarketSnapshot]) -> None:
        previous_timestamp = None
        for snapshot in history:
            if snapshot.symbol != self.profile.symbol:
                raise ValueError("market-state history does not match the asset profile")
            if (
                previous_timestamp is not None
                and snapshot.timestamp <= previous_timestamp
            ):
                raise ValueError("market-state history must be chronological")
            previous_timestamp = snapshot.timestamp

    @staticmethod
    def _window(
        history: Sequence[MarketSnapshot], hours: int
    ) -> tuple[MarketSnapshot, ...]:
        cutoff = history[-1].timestamp - timedelta(hours=hours)
        return tuple(snapshot for snapshot in history if snapshot.timestamp >= cutoff)

    @staticmethod
    def _return_rate(
        window: Sequence[MarketSnapshot],
    ) -> Decimal | None:
        if len(window) < 2:
            return None
        return (window[-1].close - window[0].close) / window[0].close

    def _assessment(
        self,
        state: MarketState,
        previous_state: MarketState,
        reason: str,
        rapid_return: Decimal,
        window: Sequence[MarketSnapshot],
    ) -> MarketStateAssessment:
        return MarketStateAssessment(
            state,
            previous_state,
            reason,
            {
                "rapid_return": str(rapid_return),
                "rapid_threshold": str(self.profile.rapid_appreciation_rate),
                "observations_in_window": str(len(window)),
            },
        )

    def _base_facts(
        self,
        window: Sequence[MarketSnapshot],
        lookback_return: Decimal | None,
        rapid_return: Decimal | None,
    ) -> dict[str, str]:
        return {
            "lookback_return": (
                "not_available" if lookback_return is None else str(lookback_return)
            ),
            "rapid_return": (
                "not_available" if rapid_return is None else str(rapid_return)
            ),
            "observations_in_window": str(len(window)),
            "uptrend_threshold": str(self.profile.uptrend_rate),
            "decline_threshold": str(self.profile.declining_rate),
        }
