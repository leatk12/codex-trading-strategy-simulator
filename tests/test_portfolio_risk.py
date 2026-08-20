from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from trading_simulator.etoro_demo import EtoroDemoError, EtoroDemoPortfolioSummary
from trading_simulator.portfolio_risk import PortfolioRiskController, PortfolioRiskPolicy


POLICY = PortfolioRiskPolicy(
    maximum_asset_exposure_usd=Decimal("1000"),
    maximum_total_exposure_usd=Decimal("2500"),
    minimum_cash_reserve_usd=Decimal("500"),
    maximum_open_assets=2,
    maximum_daily_loss_rate=Decimal("0.05"),
    maximum_drawdown_rate=Decimal("0.10"),
)


def _summary(*, cash: str = "5000", equity: str = "5000") -> EtoroDemoPortfolioSummary:
    return EtoroDemoPortfolioSummary(
        "USD", Decimal("5000"), Decimal(cash), Decimal("0"), Decimal("0"),
        Decimal(equity), 0, 0,
    )


def _payload(*positions: tuple[int, str, bool, int]) -> dict[str, object]:
    return {"clientPortfolio": {"positions": [
        {"instrumentID": instrument, "amount": amount, "isBuy": is_buy, "leverage": leverage}
        for instrument, amount, is_buy, leverage in positions
    ]}}


PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workdir():
    path = PROJECT / "outputs" / f".pytest-portfolio-risk-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def _controller(workdir) -> PortfolioRiskController:
    return PortfolioRiskController(POLICY, workdir / "portfolio-risk-state.json")


def test_allows_buy_and_records_immutable_decision(workdir) -> None:
    controller = _controller(workdir)
    result = controller.assess(
        _summary(), _payload(), instrument_id=1, proposed_buy_usd=Decimal("750"),
        reservation_id="intent-1",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )
    assert result.allowed
    assert result.state.remaining_total_capacity_usd == "1750"
    assert result.state.reserved_exposure_usd == "750"
    assert result.state.reservation_count == 1
    record = json.loads(controller.decision_log_path.read_text().splitlines()[0])
    assert record["allowed"] is True
    assert record["projected_total_exposure_usd"] == "750"


@pytest.mark.parametrize(
    ("positions", "instrument", "amount", "reason"),
    [
        (((1, "900", True, 1),), 1, "101", "per-asset exposure limit exceeded"),
        (((1, "1000", True, 1), (2, "1000", True, 1)), 3, "501", "total portfolio exposure limit exceeded"),
        (((1, "100", True, 1), (2, "100", True, 1)), 3, "10", "maximum number of open assets reached"),
    ],
)
def test_rejects_exposure_boundaries(workdir, positions, instrument, amount, reason) -> None:
    result = _controller(workdir).assess(
        _summary(), _payload(*positions), instrument_id=instrument,
        proposed_buy_usd=Decimal(amount),
        reservation_id="intent-1",
    )
    assert not result.allowed
    assert result.reason == reason


def test_rejects_cash_reserve_breach(workdir) -> None:
    result = _controller(workdir).assess(
        _summary(cash="550"), _payload(), instrument_id=1,
        proposed_buy_usd=Decimal("51"),
        reservation_id="intent-1",
    )
    assert not result.allowed
    assert result.reason == "minimum portfolio cash reserve would be breached"


@pytest.mark.parametrize(
    ("position", "message"),
    [
        ((1, "100", False, 1), "short position"),
        ((1, "100", True, 2), "leverage"),
        ((1, "0", True, 1), "non-positive position"),
    ],
)
def test_fails_closed_on_disallowed_broker_position(workdir, position, message) -> None:
    with pytest.raises(EtoroDemoError, match=message):
        _controller(workdir).assess(
            _summary(), _payload(position), instrument_id=2,
            proposed_buy_usd=Decimal("10"),
            reservation_id="intent-1",
        )


def test_daily_loss_latches_and_reset_requires_operator_audit(workdir) -> None:
    controller = _controller(workdir)
    controller.assess(
        _summary(equity="5000"), _payload(), instrument_id=1, proposed_buy_usd=None,
        observed_at=datetime(2026, 8, 20, 9, tzinfo=UTC),
    )
    halted = controller.assess(
        _summary(equity="4749"), _payload(), instrument_id=1,
        proposed_buy_usd=Decimal("10"),
        reservation_id="intent-1",
        observed_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
    )
    assert not halted.allowed
    assert halted.state.risk_halt
    still_halted = controller.assess(
        _summary(equity="5100"), _payload(), instrument_id=1,
        proposed_buy_usd=Decimal("10"),
        reservation_id="intent-2",
        observed_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
    )
    assert not still_halted.allowed
    reset = controller.reset_risk_halt(changed_by="operator", reason="reviewed")
    assert not reset.risk_halt
    assert reset.day_start_equity_usd == "5100"


def test_manual_kill_switch_blocks_buys_but_not_observation(workdir) -> None:
    controller = _controller(workdir)
    controller.assess(_summary(), _payload(), instrument_id=1, proposed_buy_usd=None)
    controller.set_manual_kill_switch(True, changed_by="operator", reason="emergency")
    blocked = controller.assess(
        _summary(), _payload(), instrument_id=1, proposed_buy_usd=Decimal("10")
        , reservation_id="intent-1"
    )
    observation = controller.assess(
        _summary(), _payload(), instrument_id=1, proposed_buy_usd=None
    )
    assert not blocked.allowed
    assert observation.allowed


def test_parallel_intents_cannot_reserve_the_same_capacity(workdir) -> None:
    controller = _controller(workdir)
    first = controller.assess(
        _summary(), _payload(), instrument_id=1, proposed_buy_usd=Decimal("1000"),
        reservation_id="btc-intent",
    )
    second = controller.assess(
        _summary(), _payload(), instrument_id=2, proposed_buy_usd=Decimal("1000"),
        reservation_id="eth-intent",
    )
    third = controller.assess(
        _summary(), _payload(), instrument_id=3, proposed_buy_usd=Decimal("501"),
        reservation_id="sol-intent",
    )
    assert first.allowed and second.allowed
    assert not third.allowed
    assert third.reason == "total portfolio exposure limit exceeded"
    assert third.state.reserved_exposure_usd == "2000"


def test_same_intent_recheck_is_idempotent_and_release_restores_capacity(workdir) -> None:
    controller = _controller(workdir)
    first = controller.assess(
        _summary(), _payload(), instrument_id=1, proposed_buy_usd=Decimal("750"),
        reservation_id="same-intent",
    )
    repeated = controller.assess(
        _summary(), _payload(), instrument_id=1, proposed_buy_usd=Decimal("750"),
        reservation_id="same-intent",
    )
    assert first.allowed and repeated.allowed
    assert repeated.state.reserved_exposure_usd == "750"
    released = controller.release_reservation(
        "same-intent", changed_by="operator", reason="intent abandoned"
    )
    assert released.reserved_exposure_usd == "0"
    assert released.remaining_total_capacity_usd == "2500"


def test_expired_reservation_is_removed_on_next_observation(workdir) -> None:
    controller = _controller(workdir)
    controller.assess(
        _summary(), _payload(), instrument_id=1, proposed_buy_usd=Decimal("1000"),
        reservation_id="expired-intent",
        observed_at=datetime(2026, 8, 20, 9, tzinfo=UTC),
    )
    later = controller.assess(
        _summary(), _payload(), instrument_id=2, proposed_buy_usd=Decimal("1000"),
        reservation_id="new-intent",
        observed_at=datetime(2026, 8, 20, 13, tzinfo=UTC),
    )
    assert later.allowed
    assert later.state.reservation_count == 1
    assert "expired-intent" not in later.state.reservations
