from decimal import Decimal
from pathlib import Path

import pytest

from trading_simulator import AssetProfile, ConfigurationError, load_asset_profile


PROJECT_ROOT = Path(__file__).parents[1]


def test_example_profile_loads_exact_decimal_values() -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / "btc_example.toml")

    assert profile.symbol == "BTC-USD"
    assert profile.strategy_version == "BTC-v1.0"
    assert profile.initial_investment == Decimal("1000.00")
    assert profile.estimated_fee_rate == Decimal("0.0025")


@pytest.mark.parametrize(
    ("filename", "symbol", "version"),
    [
        ("eth_example.toml", "ETH-USD", "ETH-v1.0"),
        ("sol_example.toml", "SOL-USD", "SOL-v1.0"),
        ("xrp_example.toml", "XRP-USD", "XRP-v1.1-15m"),
    ],
)
def test_additional_watchlist_profiles_are_independent(
    filename: str, symbol: str, version: str
) -> None:
    profile = load_asset_profile(PROJECT_ROOT / "configs" / filename)

    assert profile.symbol == symbol
    assert profile.strategy_version == version
    assert profile.initial_investment == Decimal("1000.00")
    assert profile.maximum_position_size == Decimal("2500.00")
    if symbol == "XRP-USD":
        assert profile.persistent_decline_candles == 24


def test_position_limit_cannot_be_below_initial_investment() -> None:
    values = dict(
        symbol="TEST",
        strategy_version="TEST-v1.0",
        initial_investment=Decimal("1000"),
        minimum_net_profit_rate=Decimal("0.05"),
        rapid_appreciation_rate=Decimal("0.12"),
        rapid_appreciation_window_hours=24,
        market_state_lookback_hours=3,
        uptrend_rate=Decimal("0.03"),
        declining_rate=Decimal("0.02"),
        stabilising_range_rate=Decimal("0.02"),
        recovery_rate=Decimal("0.02"),
        normal_trailing_exit_rate=Decimal("0.05"),
        momentum_trailing_exit_rate=Decimal("0.08"),
        reentry_at_previous_buy_rate=Decimal("1"),
        conservative_reentry_profit_rate=Decimal("0.10"),
        staged_reentry_profit_rate=Decimal("0.25"),
        observation_period_hours=48,
        maximum_position_size=Decimal("900"),
        scale_in_allocation_rate=Decimal("0.25"),
        scale_in_pullback_rate=Decimal("0.02"),
        scale_in_above_entry_hours=48,
        maximum_scale_in_tranches=3,
        structural_breakdown_rate=Decimal("0.35"),
        structural_peak_drawdown_rate=Decimal("0.40"),
        structural_range_lookback_hours=168,
        structural_range_break_rate=Decimal("0.20"),
        volatility_lookback_hours=24,
        extreme_volatility_rate=Decimal("0.15"),
        persistent_decline_candles=6,
        estimated_fee_rate=Decimal("0.0025"),
        estimated_spread_rate=Decimal("0.001"),
        estimated_slippage_rate=Decimal("0.001"),
    )

    with pytest.raises(ConfigurationError, match="maximum_position_size"):
        AssetProfile(**values)
