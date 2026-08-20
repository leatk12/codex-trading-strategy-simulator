from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from trading_simulator import EtoroDemoError, EtoroDryRunner, load_asset_profile
from trading_simulator.etoro_demo import EtoroDemoPortfolioSummary
from trading_simulator.etoro_live_state import EtoroLiveStateStore
from test_etoro_shadow import FakeMarketClient, _candle


PROJECT = Path(__file__).resolve().parents[1]


def _summary(open_positions: int = 0) -> EtoroDemoPortfolioSummary:
    return EtoroDemoPortfolioSummary(
        currency="USD",
        credit=Decimal("100000"),
        available_cash=Decimal("100000"),
        total_invested=Decimal("0"),
        unrealized_profit_loss=Decimal("0"),
        equity=Decimal("100000"),
        open_position_count=open_positions,
        pending_order_count=0,
    )


def _shadow():
    client = FakeMarketClient(
        {"items": [{"instrumentId": 101, "internalSymbolFull": "BTC"}]},
        {"candles": [
            _candle("2026-08-20T09:00:00Z", 99),
            _candle("2026-08-20T10:00:00Z", 100),
        ]},
    )
    return EtoroDryRunner(client).run(
        load_asset_profile(PROJECT / "configs" / "btc_example.toml"),
        symbol="BTC", resolution="one-hour", candle_count=2,
    )


def _temporary_state_path() -> Path:
    return PROJECT / "outputs" / f"test-live-state-{uuid4().hex}.json"


def test_live_state_persists_flat_baseline_and_validates_identity() -> None:
    path = _temporary_state_path()
    store = EtoroLiveStateStore(path)
    shadow = _shadow()
    try:
        state = store.initialise(shadow, _summary())
        assert store.load() == state
        assert state.baseline == shadow.latest_candle.timestamp
        with pytest.raises(EtoroDemoError, match="different strategy or instrument"):
            store.validate(state, replace(shadow, instrument_id=999))
    finally:
        path.unlink(missing_ok=True)


def test_live_state_refuses_to_guess_an_existing_broker_position() -> None:
    path = _temporary_state_path()
    store = EtoroLiveStateStore(path)

    try:
        with pytest.raises(EtoroDemoError, match="open position"):
            store.initialise(
                _shadow(),
                _summary(open_positions=1),
                {"clientPortfolio": {"positions": [{"instrumentID": 101}]}},
            )
    finally:
        path.unlink(missing_ok=True)


def test_live_state_allows_foreign_asset_position_at_initialisation() -> None:
    path = _temporary_state_path()
    store = EtoroLiveStateStore(path)
    try:
        state = store.initialise(
            _shadow(),
            _summary(open_positions=1),
            {"clientPortfolio": {"positions": [{"instrumentID": 999}]}},
        )
        assert state.instrument_id == 101
    finally:
        path.unlink(missing_ok=True)


def test_live_state_allows_new_position_after_reconciled_close() -> None:
    path = _temporary_state_path()
    store = EtoroLiveStateStore(path)
    try:
        store.initialise(_shadow(), _summary())
        store.record_open_position(
            intent_id="buy-1", position_id=10, amount_usd="1000",
            units="10", open_rate="100",
        )
        store.record_closed_position(intent_id="sell-1", position_id=10)
        reopened = store.record_open_position(
            intent_id="buy-2", position_id=20, amount_usd="1000",
            units="9", open_rate="111.11",
        )
        assert reopened.broker_position_id == 20
        assert reopened.reconciled_intent_id == "buy-2"
    finally:
        path.unlink(missing_ok=True)


def test_live_state_tracks_additional_position_lots_and_latched_liquidation() -> None:
    path = _temporary_state_path()
    store = EtoroLiveStateStore(path)
    try:
        store.initialise(_shadow(), _summary())
        store.record_open_position(
            intent_id="buy-1", position_id=10, amount_usd="1000",
            units="10", open_rate="100",
        )
        added = store.record_open_position(
            intent_id="add-1", position_id=20, amount_usd="625",
            units="5", open_rate="125",
        )
        assert added.broker_position_ids == (10, 20)
        assert added.scale_in_count == 1
        assert Decimal(added.broker_amount_usd or "0") == Decimal("1625")

        remaining = store.record_closed_position(
            intent_id="sell-1", position_id=10
        )
        assert remaining.broker_position_ids == (20,)
        assert remaining.liquidation_pending is True

        flat = store.record_closed_position(intent_id="sell-2", position_id=20)
        assert flat.broker_position_ids == ()
        assert flat.broker_position_id is None
        assert flat.liquidation_pending is False
    finally:
        path.unlink(missing_ok=True)
