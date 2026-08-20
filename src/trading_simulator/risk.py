"""Independent structural-breakdown detection for automatic-trading safety."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Mapping, Sequence

from .config import AssetProfile
from .domain import MarketSnapshot, Position


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """Explainable safeguard result for the latest historical snapshot."""

    triggered: bool
    reasons: tuple[str, ...] = ()
    facts: Mapping[str, str] = field(default_factory=dict)


class StructuralBreakdownPolicy:
    """Trigger review when any configured extreme-behaviour signal fires."""

    def __init__(self, profile: AssetProfile) -> None:
        self.profile = profile

    def assess(
        self,
        history: Sequence[MarketSnapshot],
        position: Position | None,
    ) -> RiskAssessment:
        if not history:
            raise ValueError("risk assessment requires market history")
        current = history[-1]
        reasons: list[str] = []
        facts: dict[str, str] = {}

        entry_decline = Decimal("0")
        if position is not None:
            entry_decline = max(
                (position.average_entry_price - current.close)
                / position.average_entry_price,
                Decimal("0"),
            )
            if entry_decline >= self.profile.structural_breakdown_rate:
                reasons.append("decline from all-in entry exceeded its limit")
        facts["decline_from_entry"] = str(entry_decline)
        facts["entry_decline_threshold"] = str(
            self.profile.structural_breakdown_rate
        )

        range_window = self._window(
            history, self.profile.structural_range_lookback_hours
        )
        recent_peak = max(snapshot.close for snapshot in range_window)
        peak_drawdown = max(
            (recent_peak - current.close) / recent_peak, Decimal("0")
        )
        if peak_drawdown >= self.profile.structural_peak_drawdown_rate:
            reasons.append("drawdown from the recent closing peak exceeded its limit")
        facts["recent_peak"] = str(recent_peak)
        facts["drawdown_from_recent_peak"] = str(peak_drawdown)
        facts["peak_drawdown_threshold"] = str(
            self.profile.structural_peak_drawdown_rate
        )

        prior_range = range_window[:-1]
        range_break = Decimal("0")
        if len(prior_range) >= 3:
            prior_low = min(snapshot.close for snapshot in prior_range)
            range_break = max(
                (prior_low - current.close) / prior_low, Decimal("0")
            )
            facts["prior_range_low"] = str(prior_low)
            if range_break >= self.profile.structural_range_break_rate:
                reasons.append("price broke materially below its prior closing range")
        else:
            facts["prior_range_low"] = "not_available"
        facts["break_below_prior_range"] = str(range_break)
        facts["range_break_threshold"] = str(
            self.profile.structural_range_break_rate
        )

        volatility_window = self._window(
            history, self.profile.volatility_lookback_hours
        )
        volatility = self._rolling_volatility(volatility_window)
        if (
            volatility is not None
            and volatility >= self.profile.extreme_volatility_rate
        ):
            reasons.append("rolling return volatility exceeded its limit")
        facts["rolling_volatility"] = (
            "not_available" if volatility is None else str(volatility)
        )
        facts["volatility_threshold"] = str(
            self.profile.extreme_volatility_rate
        )

        consecutive_declines = self._consecutive_declines(history)
        if consecutive_declines >= self.profile.persistent_decline_candles:
            reasons.append("consecutive declining closes exceeded their limit")
        facts["consecutive_declining_closes"] = str(consecutive_declines)
        facts["persistent_decline_threshold"] = str(
            self.profile.persistent_decline_candles
        )

        return RiskAssessment(bool(reasons), tuple(reasons), facts)

    @staticmethod
    def _window(
        history: Sequence[MarketSnapshot], hours: int
    ) -> tuple[MarketSnapshot, ...]:
        cutoff = history[-1].timestamp - timedelta(hours=hours)
        return tuple(snapshot for snapshot in history if snapshot.timestamp >= cutoff)

    @staticmethod
    def _rolling_volatility(
        window: Sequence[MarketSnapshot],
    ) -> Decimal | None:
        if len(window) < 3:
            return None
        returns = [
            (current.close - previous.close) / previous.close
            for previous, current in zip(window, window[1:])
        ]
        mean = sum(returns, Decimal("0")) / Decimal(len(returns))
        variance = sum(
            ((value - mean) ** 2 for value in returns), Decimal("0")
        ) / Decimal(len(returns))
        return variance.sqrt()

    @staticmethod
    def _consecutive_declines(history: Sequence[MarketSnapshot]) -> int:
        count = 0
        for previous, current in zip(reversed(history[:-1]), reversed(history[1:])):
            if current.close >= previous.close:
                break
            count += 1
        return count
