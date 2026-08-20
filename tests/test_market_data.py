from datetime import timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_simulator import CsvMarketDataLoader, MarketDataError


PROJECT_ROOT = Path(__file__).parents[1]


def test_loads_ordered_example_data_with_exact_decimals() -> None:
    data = CsvMarketDataLoader(
        PROJECT_ROOT / "data" / "btc_hourly_example.csv", "BTC-USD"
    ).load()

    assert len(data) == 5
    assert data.symbol == "BTC-USD"
    assert data[0].open == Decimal("42000.00")
    assert data[-1].close == Decimal("42680.00")
    assert data[0].timestamp.tzinfo == timezone.utc


def test_rejects_missing_column() -> None:
    path = FIXTURES / "missing_volume.csv"
    with pytest.raises(MarketDataError, match="missing CSV columns"):
        CsvMarketDataLoader(path, "TEST").load()


def test_rejects_naive_timestamp() -> None:
    path = FIXTURES / "naive_timestamp.csv"
    with pytest.raises(MarketDataError, match="UTC offset"):
        CsvMarketDataLoader(path, "TEST").load()


def test_rejects_duplicate_timestamps() -> None:
    path = FIXTURES / "duplicate_timestamps.csv"
    with pytest.raises(MarketDataError, match="duplicate timestamp"):
        CsvMarketDataLoader(path, "TEST").load()


def test_rejects_out_of_order_timestamps() -> None:
    path = FIXTURES / "out_of_order.csv"
    with pytest.raises(MarketDataError, match="ascending order"):
        CsvMarketDataLoader(path, "TEST").load()


def test_rejects_impossible_ohlc_relationship() -> None:
    path = FIXTURES / "impossible_high.csv"
    with pytest.raises(MarketDataError, match="high"):
        CsvMarketDataLoader(path, "TEST").load()


def test_rejects_non_finite_value() -> None:
    path = FIXTURES / "non_finite_volume.csv"
    with pytest.raises(MarketDataError, match="volume must be finite"):
        CsvMarketDataLoader(path, "TEST").load()
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
