import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from trading_simulator import (
    EtoroDemoError,
    ShadowControlStore,
    ShadowRiskEvent,
)


PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def control_path():  # type: ignore[no-untyped-def]
    directory = PROJECT / "outputs" / f".pytest-control-{uuid4()}"
    directory.mkdir(parents=True)
    try:
        yield directory / "control.json"
    finally:
        shutil.rmtree(directory)


def _event(identifier: str = "event-123") -> ShadowRiskEvent:
    return ShadowRiskEvent(
        event_id=identifier,
        triggered_at=datetime(2026, 8, 20, 8, tzinfo=UTC),
        reasons=("rolling return volatility exceeded its limit",),
        evidence={"risk_rolling_volatility": "0.20"},
    )


def test_approval_is_bound_to_one_exact_event(control_path) -> None:  # type: ignore[no-untyped-def]
    store = ShadowControlStore(control_path)
    approved_at = datetime(2026, 8, 20, 12, tzinfo=UTC)

    state = store.approve(_event(), approved_by="operator", now=approved_at)
    reloaded = store.load()

    assert state.approval_for(_event()) == _event().triggered_at
    assert reloaded.approval_for(_event()) == _event().triggered_at
    assert reloaded.approval.approved_at == approved_at
    assert reloaded.approval_for(_event("different-event")) is None


def test_sequential_event_approvals_are_preserved(control_path) -> None:  # type: ignore[no-untyped-def]
    store = ShadowControlStore(control_path)
    first = _event("first")
    second = ShadowRiskEvent(
        event_id="second",
        triggered_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
        reasons=("second breach",),
        evidence={},
    )

    store.approve(first, approved_by="operator")
    state = store.approve(second, approved_by="operator")

    assert [approval.event_id for approval in state.approvals] == ["first", "second"]
    assert state.approval_times == (first.triggered_at, second.triggered_at)


def test_kill_switch_overrides_and_invalidates_approval(control_path) -> None:  # type: ignore[no-untyped-def]
    store = ShadowControlStore(control_path)
    store.approve(_event(), approved_by="operator")

    killed = store.set_kill_switch(
        True, changed_by="operator", reason="supervised emergency stop"
    )

    assert killed.kill_switch is True
    assert killed.approval_for(_event()) is None
    assert killed.approval is None
    with pytest.raises(EtoroDemoError, match="kill switch is enabled"):
        store.approve(_event(), approved_by="operator")


def test_disabling_kill_switch_does_not_create_approval(control_path) -> None:  # type: ignore[no-untyped-def]
    store = ShadowControlStore(control_path)
    store.set_kill_switch(True, changed_by="operator", reason="stop")

    state = store.set_kill_switch(
        False, changed_by="operator", reason="investigation complete"
    )

    assert state.kill_switch is False
    assert state.approval is None
