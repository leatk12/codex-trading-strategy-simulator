import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from trading_simulator import (
    Action,
    Decision,
    EtoroDemoError,
    EtoroDryRunResult,
    EtoroResolution,
    EtoroShadowRecorder,
    MarketSnapshot,
    MarketState,
    ShadowRiskEvent,
)


def _result(hour: int) -> EtoroDryRunResult:
    timestamp = datetime(2026, 8, 20, hour, tzinfo=UTC)
    candle = MarketSnapshot(
        symbol="BTC-USD",
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("10"),
    )
    decision = Decision(
        action=Action.HOLD,
        state=MarketState.NORMAL,
        timestamp=timestamp,
        price=Decimal("101"),
        reason="No action on this completed candle.",
        facts={"evidence": "safe"},
    )
    return EtoroDryRunResult(
        strategy_version="BTC-v1.0",
        requested_symbol="BTC",
        profile_symbol="BTC-USD",
        resolution=EtoroResolution("one-hour", "OneHour", timedelta(hours=1)),
        completed_candle_count=199,
        latest_candle=candle,
        latest_decision=decision,
        proposed_action="none",
        proposed_cash_budget=None,
    )


PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def local_tmp_path():  # type: ignore[no-untyped-def]
    path = PROJECT / "outputs" / f".pytest-shadow-{uuid4()}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def test_recorder_appends_each_completed_candle_only_once(local_tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = local_tmp_path / "shadow" / "btc.jsonl"
    recorder = EtoroShadowRecorder(path)

    first = recorder.record(
        _result(10), observed_at=datetime(2026, 8, 20, 11, tzinfo=UTC)
    )
    duplicate = recorder.record(_result(10))
    second = recorder.record(_result(11))

    assert first.recorded is True
    assert duplicate.recorded is False
    assert second.recorded is True
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["strategy_version"] == "BTC-v1.0"
    assert rows[0]["order_submitted"] is False
    assert rows[0]["borrowing_allowed"] is False
    assert rows[0]["leverage"] == 1


def test_recorder_resumes_without_duplicating_final_record(local_tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = local_tmp_path / "btc.jsonl"
    EtoroShadowRecorder(path).record(_result(10))

    resumed = EtoroShadowRecorder(path)

    assert resumed.record(_result(10)).recorded is False
    assert resumed.record(_result(11)).recorded is True


def test_recorder_refuses_corrupt_existing_log(local_tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = local_tmp_path / "btc.jsonl"
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(EtoroDemoError, match="invalid final record"):
        EtoroShadowRecorder(path)


def test_recorder_appends_changed_risk_event_on_same_candle(local_tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = local_tmp_path / "btc.jsonl"
    first = ShadowRiskEvent(
        "first", datetime(2026, 8, 20, 8, tzinfo=UTC), ("first",), {}
    )
    second = ShadowRiskEvent(
        "second", datetime(2026, 8, 20, 9, tzinfo=UTC), ("second",), {}
    )
    recorder = EtoroShadowRecorder(path)

    assert recorder.record(replace(_result(10), active_risk_event=first)).recorded
    assert recorder.record(replace(_result(10), active_risk_event=second)).recorded
    assert not recorder.record(replace(_result(10), active_risk_event=second)).recorded
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["active_risk_event"]["event_id"] for row in rows] == [
        "first",
        "second",
    ]
