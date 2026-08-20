from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_simulator import EtoroDemoError, EtoroDryRunner, load_asset_profile
from trading_simulator.etoro_shadow import _timestamp
from trading_simulator.etoro_shadow import _active_risk_event
from trading_simulator import Action, Decision, MarketState
from decimal import Decimal


PROJECT = Path(__file__).resolve().parents[1]


class FakeMarketClient:
    def __init__(self, search, candles):  # type: ignore[no-untyped-def]
        self.search = search
        self.candles = candles
        self.calls = []

    def market_data(self, resource, parameters=None):  # type: ignore[no-untyped-def]
        self.calls.append((resource, parameters))
        return self.search if resource == "search" else self.candles


def _candle(timestamp: str, close: int):
    return {
        "fromDate": timestamp,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 10,
    }


def test_dry_run_resolves_exact_symbol_and_drops_forming_candle() -> None:
    client = FakeMarketClient(
        {
            "items": [
                {"instrumentId": 100, "internalSymbolFull": "BTCOLD"},
                {"instrumentId": 101, "internalSymbolFull": "BTC"},
            ]
        },
        {
            "candles": [
                _candle("2026-08-20T10:00:00Z", 100),
                _candle("2026-08-20T09:00:00Z", 99),
                _candle("2026-08-20T11:00:00Z", 101),
            ]
        },
    )
    runner = EtoroDryRunner(
        client, now=datetime(2026, 8, 20, 11, 30, tzinfo=UTC)
    )

    result = runner.run(
        load_asset_profile(PROJECT / "configs" / "btc_example.toml"),
        symbol="BTC",
        resolution="one-hour",
        candle_count=3,
    )

    assert result.completed_candle_count == 2
    assert result.latest_candle.timestamp == datetime(2026, 8, 20, 10, tzinfo=UTC)
    assert result.order_submitted is False
    assert result.leverage == 1
    assert client.calls[0] == (
        "search",
        {
            "internalSymbolFull": "BTC",
            "fields": "instrumentId,internalSymbolFull,displayname",
        },
    )
    assert client.calls[1][0] == (
        "instruments/101/history/candles/desc/OneHour/3"
    )


def test_dry_run_accepts_live_nested_candle_envelope() -> None:
    client = FakeMarketClient(
        {"items": [{"instrumentId": 101, "internalSymbolFull": "BTC"}]},
        {
            "interval": "OneHour",
            "candles": [
                {
                    "instrumentId": 101,
                    "candles": [
                        _candle("2026-08-20T09:00:00Z", 99),
                        _candle("2026-08-20T10:00:00Z", 100),
                    ],
                    "rangeOpen": 99,
                }
            ],
        },
    )

    result = EtoroDryRunner(
        client, now=datetime(2026, 8, 20, 12, tzinfo=UTC)
    ).run(
        load_asset_profile(PROJECT / "configs" / "btc_example.toml"),
        symbol="BTC",
        resolution="one-hour",
        candle_count=200,
    )

    assert result.completed_candle_count == 2
    assert result.latest_candle.close == 100


def test_dry_run_rejects_ambiguous_exact_symbol() -> None:
    client = FakeMarketClient(
        {
            "items": [
                {"instrumentId": 1, "internalSymbolFull": "BTC"},
                {"instrumentId": 2, "internalSymbolFull": "btc"},
            ]
        },
        {"candles": []},
    )

    with pytest.raises(EtoroDemoError, match="received 2"):
        EtoroDryRunner(client).run(
            load_asset_profile(PROJECT / "configs" / "btc_example.toml"),
            symbol="BTC",
            resolution="one-hour",
            candle_count=200,
        )


@pytest.mark.parametrize("count", [1, 1001])
def test_dry_run_enforces_candle_limit(count: int) -> None:
    client = FakeMarketClient({"items": []}, {"candles": []})

    with pytest.raises(EtoroDemoError, match="between 2 and 1000"):
        EtoroDryRunner(client).run(
            load_asset_profile(PROJECT / "configs" / "btc_example.toml"),
            symbol="BTC",
            resolution="one-hour",
            candle_count=count,
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1787216400, datetime(2026, 8, 20, 9, tzinfo=UTC)),
        (1787216400000, datetime(2026, 8, 20, 9, tzinfo=UTC)),
        (1787216400000000, datetime(2026, 8, 20, 9, tzinfo=UTC)),
    ],
)
def test_candle_timestamp_accepts_unix_seconds_milliseconds_and_microseconds(
    raw: int, expected: datetime
) -> None:
    assert _timestamp(raw, 1) == expected


def test_candle_timestamp_rejects_implausible_numeric_value() -> None:
    with pytest.raises(EtoroDemoError, match="outside the supported"):
        _timestamp(42, 1)


def test_active_risk_event_is_stable_and_cleared_only_by_approval() -> None:
    triggered_at = datetime(2026, 8, 20, 8, tzinfo=UTC)
    trigger = Decision(
        action=Action.SUSPEND_AUTOMATIC_BUYING,
        state=MarketState.MANUAL_REVIEW,
        timestamp=triggered_at,
        price=Decimal("100"),
        reason="risk",
        facts={
            "risk_triggered_on_candle": "true",
            "manual_approval_on_candle": "false",
            "risk_reasons": "rolling return volatility exceeded its limit",
            "risk_rolling_volatility": "0.20",
        },
    )
    event = _active_risk_event((trigger,), "BTC-v1.0", "BTC-USD")
    approval = Decision(
        action=Action.HOLD,
        state=MarketState.NORMAL,
        timestamp=datetime(2026, 8, 20, 9, tzinfo=UTC),
        price=Decimal("101"),
        reason="approved",
        facts={"manual_approval_on_candle": "true"},
    )

    assert event is not None
    assert len(event.event_id) == 24
    assert event.triggered_at == triggered_at
    assert _active_risk_event((trigger,), "BTC-v1.0", "BTC-USD") == event
    assert _active_risk_event((trigger, approval), "BTC-v1.0", "BTC-USD") is None
