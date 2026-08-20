import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from trading_simulator import (
    Action,
    Decision,
    EtoroDemoPortfolioSummary,
    EtoroDryRunResult,
    EtoroIntentBuilder,
    EtoroResolution,
    IntentAuditWriter,
    IntentConstraints,
    IntentReadinessResult,
    MarketSnapshot,
    MarketState,
    ShadowControlState,
    ReadinessAuditWriter,
    load_asset_profile,
)


PROJECT = Path(__file__).resolve().parents[1]


def _shadow(action: Action = Action.BUY) -> EtoroDryRunResult:
    timestamp = datetime(2026, 8, 20, 10, tzinfo=UTC)
    snapshot = MarketSnapshot(
        "BTC-USD",
        timestamp,
        Decimal("70000"),
        Decimal("71000"),
        Decimal("69000"),
        Decimal("70500"),
        Decimal("100"),
    )
    decision = Decision(
        action,
        MarketState.UPTREND,
        timestamp,
        Decimal("70500"),
        "test decision",
        cash_budget=Decimal("1000") if action is Action.BUY else None,
    )
    return EtoroDryRunResult(
        strategy_version="BTC-v1.0",
        requested_symbol="BTC",
        profile_symbol="BTC-USD",
        resolution=EtoroResolution("one-hour", "OneHour", timedelta(hours=1)),
        completed_candle_count=200,
        latest_candle=snapshot,
        latest_decision=decision,
        proposed_action="buy" if action is Action.BUY else "none",
        proposed_cash_budget=Decimal("1000") if action is Action.BUY else None,
        instrument_id=100,
        simulated_position_open=action is Action.BUY,
    )


def _summary() -> EtoroDemoPortfolioSummary:
    return EtoroDemoPortfolioSummary(
        "USD",
        Decimal("100000"),
        Decimal("100000"),
        Decimal("0"),
        Decimal("0"),
        Decimal("100000"),
        0,
        0,
    )


def _constraints() -> IntentConstraints:
    return IntentConstraints(Decimal("10"), Decimal("0.01"), timedelta(minutes=90))


def _empty_pnl():  # type: ignore[no-untyped-def]
    return {
        "clientPortfolio": {
            "positions": [],
            "ordersForOpen": [],
            "orders": [],
            "mirrors": [],
        }
    }


def test_buy_intent_is_cash_only_one_x_and_never_submitted() -> None:
    result = EtoroIntentBuilder(
        now=datetime(2026, 8, 20, 11, 30, tzinfo=UTC)
    ).build(
        load_asset_profile(PROJECT / "configs" / "btc_example.toml"),
        _shadow(),
        _summary(),
        _empty_pnl(),
        ShadowControlState(),
        _constraints(),
    )

    assert result.ready
    assert result.intent is not None
    assert result.intent.request_body == {
        "action": "open",
        "transaction": "buy",
        "instrumentId": 100,
        "orderType": "mkt",
        "amount": "1000",
        "orderCurrency": "usd",
        "leverage": 1,
    }
    assert result.intent.order_submitted is False


@pytest.mark.parametrize(
    ("pnl", "reason"),
    [
        (
            {
                "clientPortfolio": {
                    "positions": [],
                    "ordersForOpen": [{"amount": 10}],
                    "orders": [],
                    "mirrors": [],
                }
            },
            "pending orders",
        ),
        (
            {
                "clientPortfolio": {
                    "positions": [],
                    "ordersForOpen": [],
                    "orders": [],
                    "mirrors": [{"positions": []}],
                }
            },
            "copy/mirror",
        ),
    ],
)
def test_intent_refuses_pending_orders_and_mirror_exposure(pnl, reason) -> None:  # type: ignore[no-untyped-def]
    result = EtoroIntentBuilder(
        now=datetime(2026, 8, 20, 11, 30, tzinfo=UTC)
    ).build(
        load_asset_profile(PROJECT / "configs" / "btc_example.toml"),
        _shadow(),
        _summary(),
        pnl,
        ShadowControlState(),
        _constraints(),
    )
    assert not result.ready
    assert reason in result.reason


def test_intent_refuses_stale_candle() -> None:
    result = EtoroIntentBuilder(
        now=datetime(2026, 8, 20, 13, tzinfo=UTC)
    ).build(
        load_asset_profile(PROJECT / "configs" / "btc_example.toml"),
        _shadow(),
        _summary(),
        _empty_pnl(),
        ShadowControlState(),
        _constraints(),
    )
    assert not result.ready
    assert "stale" in result.reason


def test_intent_audit_deduplicates_idempotency_key() -> None:
    directory = PROJECT / "outputs" / f".pytest-intent-{uuid4()}"
    path = directory / "intents.jsonl"
    try:
        result = EtoroIntentBuilder(
            now=datetime(2026, 8, 20, 11, 30, tzinfo=UTC)
        ).build(
            load_asset_profile(PROJECT / "configs" / "btc_example.toml"),
            _shadow(),
            _summary(),
            _empty_pnl(),
            ShadowControlState(),
            _constraints(),
        )
        writer = IntentAuditWriter(path)
        assert writer.append(result)
        assert not writer.append(result)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["order_submitted"] is False
        assert records[0]["leverage"] == 1
    finally:
        if directory.exists():
            shutil.rmtree(directory)


def test_readiness_audit_records_rejection_once_per_candle() -> None:
    directory = PROJECT / "outputs" / f".pytest-readiness-{uuid4()}"
    path = directory / "readiness.jsonl"
    try:
        writer = ReadinessAuditWriter(path)
        shadow = _shadow(Action.HOLD)
        outcome = IntentReadinessResult(
            False, "latest strategy decision requires no order"
        )
        assert writer.append(shadow, outcome, _summary())
        assert not writer.append(shadow, outcome, _summary())
        assert writer.has_candle(
            shadow.strategy_version,
            shadow.requested_symbol,
            shadow.latest_candle.timestamp,
        )
        record = json.loads(path.read_text().strip())
        assert record["ready"] is False
        assert record["halt_monitor"] is False
        assert record["order_submitted"] is False
        report = writer.report()
        assert report.evaluations == 1
        assert report.ready == 0
        assert report.rejected == 1
        assert report.halting_failures == 0
    finally:
        if directory.exists():
            shutil.rmtree(directory)
