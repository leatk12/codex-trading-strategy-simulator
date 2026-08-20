"""Asset-specific strategy configuration and TOML loading."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tomllib
from typing import Any


class ConfigurationError(ValueError):
    """Raised when an asset profile is missing or financially inconsistent."""


@dataclass(frozen=True, slots=True)
class AssetProfile:
    """Manually versioned assumptions for one asset's strategy."""

    symbol: str
    strategy_version: str
    initial_investment: Decimal
    minimum_net_profit_rate: Decimal
    rapid_appreciation_rate: Decimal
    rapid_appreciation_window_hours: int
    market_state_lookback_hours: int
    uptrend_rate: Decimal
    declining_rate: Decimal
    stabilising_range_rate: Decimal
    recovery_rate: Decimal
    normal_trailing_exit_rate: Decimal
    momentum_trailing_exit_rate: Decimal
    reentry_at_previous_buy_rate: Decimal
    conservative_reentry_profit_rate: Decimal
    staged_reentry_profit_rate: Decimal
    observation_period_hours: int
    maximum_position_size: Decimal
    structural_breakdown_rate: Decimal
    structural_peak_drawdown_rate: Decimal
    structural_range_lookback_hours: int
    structural_range_break_rate: Decimal
    volatility_lookback_hours: int
    extreme_volatility_rate: Decimal
    persistent_decline_candles: int
    estimated_fee_rate: Decimal
    estimated_spread_rate: Decimal
    estimated_slippage_rate: Decimal

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ConfigurationError("symbol must not be empty")
        if not self.strategy_version.strip():
            raise ConfigurationError("strategy_version must not be empty")
        if self.initial_investment <= 0:
            raise ConfigurationError("initial_investment must be greater than zero")
        if self.maximum_position_size < self.initial_investment:
            raise ConfigurationError(
                "maximum_position_size must be at least initial_investment"
            )
        if self.rapid_appreciation_window_hours <= 0:
            raise ConfigurationError(
                "rapid_appreciation_window_hours must be greater than zero"
            )
        if self.market_state_lookback_hours <= 0:
            raise ConfigurationError(
                "market_state_lookback_hours must be greater than zero"
            )
        if self.structural_range_lookback_hours <= 0:
            raise ConfigurationError(
                "structural_range_lookback_hours must be greater than zero"
            )
        if self.volatility_lookback_hours <= 0:
            raise ConfigurationError(
                "volatility_lookback_hours must be greater than zero"
            )
        if self.persistent_decline_candles < 2:
            raise ConfigurationError(
                "persistent_decline_candles must be at least 2"
            )
        if self.observation_period_hours < 0:
            raise ConfigurationError("observation_period_hours must not be negative")

        rate_names = (
            "minimum_net_profit_rate",
            "rapid_appreciation_rate",
            "uptrend_rate",
            "declining_rate",
            "stabilising_range_rate",
            "recovery_rate",
            "normal_trailing_exit_rate",
            "momentum_trailing_exit_rate",
            "reentry_at_previous_buy_rate",
            "conservative_reentry_profit_rate",
            "staged_reentry_profit_rate",
            "structural_breakdown_rate",
            "structural_peak_drawdown_rate",
            "structural_range_break_rate",
            "extreme_volatility_rate",
            "estimated_fee_rate",
            "estimated_spread_rate",
            "estimated_slippage_rate",
        )
        for name in rate_names:
            value = getattr(self, name)
            if not Decimal("0") <= value <= Decimal("1"):
                raise ConfigurationError(f"{name} must be between 0 and 1")


_DECIMAL_FIELDS = {
    field.name for field in fields(AssetProfile) if field.type == "Decimal"
}


def _as_decimal(name: str, value: Any) -> Decimal:
    if isinstance(value, float):
        raise ConfigurationError(
            f"{name} must be a quoted TOML string, not a floating-point number"
        )
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ConfigurationError(f"{name} must be a decimal number") from error


def load_asset_profile(path: str | Path) -> AssetProfile:
    """Read and validate one TOML asset profile."""

    profile_path = Path(path)
    try:
        with profile_path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"could not read {profile_path}: {error}") from error

    expected = {field.name for field in fields(AssetProfile)}
    missing = expected - raw.keys()
    unknown = raw.keys() - expected
    if missing:
        raise ConfigurationError(f"missing configuration keys: {sorted(missing)}")
    if unknown:
        raise ConfigurationError(f"unknown configuration keys: {sorted(unknown)}")

    values = dict(raw)
    for name in _DECIMAL_FIELDS:
        values[name] = _as_decimal(name, values[name])

    try:
        return AssetProfile(**values)
    except TypeError as error:
        raise ConfigurationError(f"invalid configuration types: {error}") from error
