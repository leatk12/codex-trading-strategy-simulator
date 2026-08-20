"""Historical market-data loading and validation.

pandas is deliberately confined to this boundary module. Strategies consume
MarketSnapshot objects and do not know how the source data was stored.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, overload

import pandas as pd

from .domain import MarketSnapshot


class MarketDataError(ValueError):
    """Raised when historical data cannot be trusted by the simulator."""


class MarketDataSource(Protocol):
    """Contract implemented by CSV and future API-backed data sources."""

    def load(self) -> "HistoricalMarketData":
        """Return a fully validated chronological market-data series."""


class HistoricalMarketData(Sequence[MarketSnapshot]):
    """An immutable, non-empty, chronological sequence of OHLCV candles."""

    def __init__(self, snapshots: Sequence[MarketSnapshot]) -> None:
        self._snapshots = tuple(snapshots)
        self._validate_series()

    def _validate_series(self) -> None:
        if not self._snapshots:
            raise MarketDataError("historical market data must not be empty")

        expected_symbol = self._snapshots[0].symbol
        previous_timestamp: datetime | None = None
        for row_number, snapshot in enumerate(self._snapshots, start=1):
            if snapshot.symbol != expected_symbol:
                raise MarketDataError(
                    f"row {row_number}: expected symbol {expected_symbol!r}, "
                    f"found {snapshot.symbol!r}"
                )
            if previous_timestamp is not None:
                if snapshot.timestamp == previous_timestamp:
                    raise MarketDataError(
                        f"row {row_number}: duplicate timestamp {snapshot.timestamp.isoformat()}"
                    )
                if snapshot.timestamp < previous_timestamp:
                    raise MarketDataError(
                        f"row {row_number}: timestamps must be in ascending order"
                    )
            previous_timestamp = snapshot.timestamp

    @property
    def symbol(self) -> str:
        return self._snapshots[0].symbol

    @property
    def start(self) -> datetime:
        return self._snapshots[0].timestamp

    @property
    def end(self) -> datetime:
        return self._snapshots[-1].timestamp

    def __len__(self) -> int:
        return len(self._snapshots)

    @overload
    def __getitem__(self, index: int) -> MarketSnapshot: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[MarketSnapshot, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> MarketSnapshot | tuple[MarketSnapshot, ...]:
        return self._snapshots[index]

    def __iter__(self) -> Iterator[MarketSnapshot]:
        return iter(self._snapshots)


class CsvMarketDataLoader:
    """Load one asset's ISO-8601 timestamped OHLCV candles from a CSV file."""

    REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

    def __init__(self, path: str | Path, symbol: str) -> None:
        self.path = Path(path)
        self.symbol = symbol.strip()
        if not self.symbol:
            raise MarketDataError("symbol must not be empty")

    def load(self) -> HistoricalMarketData:
        try:
            # dtype=str preserves the source decimal text. Converting prices to
            # float first would introduce binary floating-point approximation.
            frame = pd.read_csv(self.path, dtype=str, keep_default_na=False)
        except (OSError, pd.errors.ParserError) as error:
            raise MarketDataError(f"could not read {self.path}: {error}") from error

        self._validate_columns(frame)
        snapshots = [
            self._snapshot_from_row(row_number, row)
            for row_number, (_, row) in enumerate(frame.iterrows(), start=2)
        ]
        return HistoricalMarketData(snapshots)

    def _validate_columns(self, frame: pd.DataFrame) -> None:
        actual = set(frame.columns)
        required = set(self.REQUIRED_COLUMNS)
        missing = required - actual
        unknown = actual - required
        if missing:
            raise MarketDataError(f"missing CSV columns: {sorted(missing)}")
        if unknown:
            raise MarketDataError(f"unknown CSV columns: {sorted(unknown)}")
        if frame.empty:
            raise MarketDataError("CSV contains no market-data rows")

    def _snapshot_from_row(
        self, row_number: int, row: pd.Series
    ) -> MarketSnapshot:
        timestamp = self._parse_timestamp(row_number, row["timestamp"])
        values = {
            name: self._parse_decimal(row_number, name, row[name])
            for name in ("open", "high", "low", "close", "volume")
        }
        try:
            return MarketSnapshot(
                symbol=self.symbol,
                timestamp=timestamp,
                open=values["open"],
                high=values["high"],
                low=values["low"],
                close=values["close"],
                volume=values["volume"],
            )
        except ValueError as error:
            raise MarketDataError(f"CSV row {row_number}: {error}") from error

    @staticmethod
    def _parse_timestamp(row_number: int, value: str) -> datetime:
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError) as error:
            raise MarketDataError(
                f"CSV row {row_number}: invalid timestamp {value!r}"
            ) from error
        if timestamp.tzinfo is None:
            raise MarketDataError(
                f"CSV row {row_number}: timestamp must include a UTC offset"
            )
        # Normalising to UTC makes timestamps from different offsets comparable.
        return timestamp.tz_convert("UTC").to_pydatetime()

    @staticmethod
    def _parse_decimal(row_number: int, name: str, value: str) -> Decimal:
        try:
            number = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise MarketDataError(
                f"CSV row {row_number}: {name} must be a decimal number"
            ) from error
        if not number.is_finite():
            raise MarketDataError(
                f"CSV row {row_number}: {name} must be finite"
            )
        return number

