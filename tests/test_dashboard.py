from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
from decimal import Decimal
import json
import shutil
from uuid import uuid4

import pytest

from trading_simulator.dashboard import (
    AUTOMATION_ARMING_PHRASE,
    DashboardAuthenticator,
    DashboardActions,
    DashboardData,
    DashboardMonitorManager,
    DemoAutomationCoordinator,
    MONITOR_SPECS,
    _execution_summary,
    _last_monitor_halt,
    hash_dashboard_password,
    verify_dashboard_password,
)
from trading_simulator.etoro_demo import EtoroDemoError
from trading_simulator.etoro_demo import EtoroDemoPortfolioSummary
from trading_simulator.shadow_control import ShadowControlState, ShadowControlStore


FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = Path(__file__).resolve().parents[1]


def test_dashboard_password_hash_is_salted_and_verifiable() -> None:
    first = hash_dashboard_password("a-long-test-password")
    second = hash_dashboard_password("a-long-test-password")

    assert first != second
    assert verify_dashboard_password("a-long-test-password", first)
    assert not verify_dashboard_password("wrong-password", first)
    assert not verify_dashboard_password("a-long-test-password", "malformed")


def test_dashboard_authenticator_issues_and_revokes_session() -> None:
    auth = DashboardAuthenticator(
        "operator", hash_dashboard_password("a-long-test-password")
    )

    assert auth.login("operator", "wrong-password", "127.0.0.1") is None
    token = auth.login("operator", "a-long-test-password", "127.0.0.1")
    assert token is not None
    assert auth.authenticated(token)
    auth.logout(token)
    assert not auth.authenticated(token)


def test_dashboard_authenticator_rate_limits_repeated_failures() -> None:
    auth = DashboardAuthenticator(
        "operator", hash_dashboard_password("a-long-test-password")
    )
    for _ in range(5):
        assert auth.login("operator", "wrong-password", "127.0.0.1") is None

    assert auth.login("operator", "a-long-test-password", "127.0.0.1") is None


@pytest.fixture
def automation_dir():  # type: ignore[no-untyped-def]
    directory = PROJECT / "outputs" / f".pytest-automation-{uuid4().hex}"
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def test_dashboard_reads_latest_asset_state() -> None:
    btc = DashboardData(FIXTURES / "dashboard_valid").snapshot().assets[0]

    assert btc["asset"] == "BTC"
    assert btc["candle_timestamp"] == "new"
    assert btc["decision"] == "hold"
    assert btc["ready"] is False
    assert btc["order_submitted"] is False
    assert btc["leverage"] == 1
    assert btc["resolution"] == "one-hour"
    assert btc["intent_count"] == 1
    assert btc["can_rebaseline"] is False


def test_xrp_dashboard_monitor_uses_isolated_fifteen_minute_stream() -> None:
    spec = MONITOR_SPECS["xrp"]

    assert spec.resolution == "fifteen-minutes"
    assert spec.stem == "xrp-fifteen-minutes"
    assert spec.candle_count == 800
    assert spec.poll_seconds == 60


def test_dashboard_tolerates_missing_and_malformed_logs() -> None:
    snapshot = DashboardData(FIXTURES / "dashboard_malformed").snapshot()
    btc, eth = snapshot.assets[:2]

    assert btc["available"] is False
    assert eth["error"].startswith("invalid eth-readiness.jsonl")
    assert snapshot.safety["order_execution"] == "explicitly armed Demo only"
    assert snapshot.safety["real_account_access"] == "BLOCKED"
    assert snapshot.safety["borrowing_allowed"] is False


def test_dashboard_approval_is_bound_to_exact_active_event() -> None:
    actions = DashboardActions(FIXTURES / "dashboard_risk")
    with patch.object(
        ShadowControlStore, "approve", return_value=ShadowControlState()
    ) as approve:
        result = actions.approve("btc", "risk-123", "operator")

    assert result["action"] == "approved"
    assert result["order_submitted"] is False
    approve.assert_called_once()

    with pytest.raises(EtoroDemoError, match="event changed"):
        actions.approve("btc", "wrong-event", "operator")


def test_dashboard_refusal_enables_kill_switch_without_order() -> None:
    actions = DashboardActions(FIXTURES / "dashboard_risk")
    with patch.object(
        ShadowControlStore,
        "set_kill_switch",
        return_value=ShadowControlState(kill_switch=True),
    ) as halt:
        result = actions.refuse("btc", "risk-123", "operator")

    assert result["action"] == "refused_and_halted"
    assert result["kill_switch"] is True
    assert result["order_submitted"] is False
    assert halt.call_args.args == (True,)


def test_dashboard_can_explicitly_disable_kill_switch() -> None:
    actions = DashboardActions(FIXTURES / "dashboard_risk")
    enabled = ShadowControlState(kill_switch=True)
    disabled = ShadowControlState(kill_switch=False)
    with (
        patch.object(ShadowControlStore, "load", return_value=enabled),
        patch.object(
            ShadowControlStore, "set_kill_switch", return_value=disabled
        ) as change,
    ):
        result = actions.reenable("xrp", "operator")

    assert result["action"] == "monitoring_reenabled"
    assert result["kill_switch"] is False
    assert result["order_submitted"] is False
    assert change.call_args.args == (False,)


def test_dashboard_can_abandon_unattempted_intent_only_when_demo_is_flat() -> None:
    actions = DashboardActions(FIXTURES / "dashboard_valid")
    intent = {
        "environment": "etoro_demo",
        "execution_eligible": True,
        "order_submitted": False,
        "intent_id": "intent-123",
        "strategy_version": "XRP-v1.1-15m",
    }
    shadow = {"candle_timestamp": "2026-08-20T15:00:00+00:00"}
    client = SimpleNamespace(
        demo_pnl=lambda: {"clientPortfolio": {
            "positions": [], "orders": [], "ordersForOpen": [], "mirrors": []
        }},
        demo_summary=lambda payload: EtoroDemoPortfolioSummary(
            currency="USD", credit=Decimal("100000"),
            available_cash=Decimal("100000"), total_invested=Decimal("0"),
            unrealized_profit_loss=Decimal("0"), equity=Decimal("100000"),
            open_position_count=0, pending_order_count=0,
        ),
    )
    state = SimpleNamespace(
        generation=2,
        baseline_candle_timestamp="2026-08-20T15:00:00+00:00",
    )
    with (
        patch(
            "trading_simulator.dashboard._last_json",
            side_effect=lambda path: intent if "intents" in path.name else shadow,
        ),
        patch("trading_simulator.dashboard.ExecutionLedger.has_attempt", return_value=False),
        patch("trading_simulator.dashboard.EtoroCredentials.from_environment"),
        patch("trading_simulator.dashboard.EtoroDemoReadOnlyClient", return_value=client),
        patch("trading_simulator.dashboard._append_jsonl_once") as audit,
            patch(
                "trading_simulator.dashboard.EtoroLiveStateStore.rebaseline_flat",
                return_value=state,
            ) as rebaseline,
            patch(
                "trading_simulator.dashboard.EtoroLiveStateStore.load",
                return_value=SimpleNamespace(instrument_id=100003),
            ),
    ):
        result = actions.abandon_and_rebaseline("xrp", "operator")

    assert result["action"] == "intent_abandoned_and_rebaselined"
    assert result["order_submitted"] is False
    audit.assert_called_once()
    rebaseline.assert_called_once()


def test_dashboard_monitor_start_requires_inherited_credentials() -> None:
    manager = DashboardMonitorManager(FIXTURES, FIXTURES / "dashboard_valid")

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(EtoroDemoError, match="has no eToro keys"):
            manager.start("btc")


def test_demo_automation_requires_explicit_acknowledgement(automation_dir) -> None:  # type: ignore[no-untyped-def]
    actions = DashboardActions(automation_dir, PROJECT)

    with pytest.raises(EtoroDemoError, match="acknowledgement"):
        actions.set_demo_automation(True, "operator", "wrong")

    enabled = actions.set_demo_automation(
        True, "operator", AUTOMATION_ARMING_PHRASE
    )
    assert enabled["enabled"] is True
    assert enabled["environment"] == "etoro_demo"
    assert enabled["real_account_allowed"] is False
    assert enabled["leverage"] == 1


def test_demo_automation_executes_routine_intent_without_per_order_approval(automation_dir) -> None:  # type: ignore[no-untyped-def]
    actions = Mock()
    monitors = Mock()
    monitors.pause_for_execution.return_value = True
    coordinator = DemoAutomationCoordinator(Mock(), actions, monitors, automation_dir)

    coordinator._process_asset({
        "asset": "BTC",
        "can_execute_demo": True,
        "intent_id": "routine-intent",
        "automation_review_required": False,
        "reconciliation_pending": False,
    })

    actions.execute_demo_intent.assert_called_once_with(
        "btc",
        "routine-intent",
        "rule-based-demo-automation",
        "I_UNDERSTAND_THIS_SUBMITS_A_DEMO_ORDER",
        "1000.00",
    )
    monitors.pause_for_execution.assert_called_once_with("btc")


def test_demo_automation_defers_unusual_climb_exit_to_operator(automation_dir) -> None:  # type: ignore[no-untyped-def]
    actions = Mock()
    monitors = Mock()
    coordinator = DemoAutomationCoordinator(Mock(), actions, monitors, automation_dir)

    coordinator._process_asset({
        "asset": "XRP",
        "can_execute_demo": True,
        "intent_id": "unusual-exit",
        "automation_review_required": True,
        "automation_review_reason": "unusual climb",
        "reconciliation_pending": False,
    })

    actions.execute_demo_intent.assert_not_called()
    monitors.pause_for_execution.assert_not_called()
    audit = (automation_dir / "demo-automation-audit.jsonl").read_text(encoding="utf-8")
    assert "operator_approval_required" in audit
    assert "unusual-exit" in audit


@pytest.mark.parametrize("safety_flag", ["risk_active", "kill_switch"])
def test_demo_automation_never_executes_through_asset_safety_latch(
    automation_dir, safety_flag: str  # type: ignore[no-untyped-def]
) -> None:
    actions = Mock()
    monitors = Mock()
    coordinator = DemoAutomationCoordinator(Mock(), actions, monitors, automation_dir)
    asset = {
        "asset": "ETH",
        "can_execute_demo": True,
        "intent_id": "blocked-intent",
        "automation_review_required": False,
        "reconciliation_pending": False,
        safety_flag: True,
    }

    coordinator._process_asset(asset)

    actions.execute_demo_intent.assert_not_called()
    monitors.pause_for_execution.assert_not_called()


def test_dashboard_pauses_only_for_execution_and_resumes_after_reconciliation() -> None:
    manager = DashboardMonitorManager(FIXTURES, FIXTURES / "dashboard_valid")
    with (
        patch.object(manager, "running", return_value=True),
        patch.object(manager, "stop") as stop,
    ):
        assert manager.pause_for_execution("btc") is True
    stop.assert_called_once_with("btc")

    with patch.object(manager, "start") as start:
        assert manager.resume_after_reconciliation("btc") is True
    start.assert_called_once_with("btc")


def test_dashboard_reads_last_child_monitor_halt() -> None:
    assert _last_monitor_halt(FIXTURES / "dashboard-monitor-halt.log") == (
        "HALTED safely: eToro read failed (TimeoutError); credentials were redacted"
    )


def test_dashboard_expands_generic_operator_review_to_exact_cause() -> None:
    directory = PROJECT / "outputs" / f".pytest-dashboard-halt-{uuid4().hex}"
    directory.mkdir(parents=True)
    path = directory / "monitor.log"
    try:
        path.write_text(
            "    Demo portfolio and simulator position state do not reconcile\n"
            "HALTED safely; operator review is required.\n",
            encoding="utf-8",
        )
        assert _last_monitor_halt(path) == (
            "HALTED safely: Demo portfolio and simulator position state do not reconcile"
        )
    finally:
        shutil.rmtree(directory)


def test_dashboard_does_not_reuse_halt_from_an_earlier_monitor_session() -> None:
    directory = PROJECT / "outputs" / f".pytest-dashboard-session-{uuid4().hex}"
    directory.mkdir(parents=True)
    path = directory / "monitor.log"
    try:
        path.write_text(
            "HALTED safely: old failure\n"
            "eToro Demo continuous execution-readiness monitor\n"
            "2026-08-20T18:00:00+00:00 | already evaluated | submitted=NO\n",
            encoding="utf-8",
        )
        assert _last_monitor_halt(path) is None
    finally:
        shutil.rmtree(directory)


def test_dashboard_flat_rebaseline_preserves_other_demo_assets() -> None:
    actions = DashboardActions(FIXTURES / "dashboard_valid")
    client = SimpleNamespace(
        demo_pnl=lambda: {"clientPortfolio": {
            "positions": [{"instrumentID": 999, "isBuy": True, "leverage": 1}],
            "mirrors": [],
        }},
        demo_summary=lambda payload: EtoroDemoPortfolioSummary(
            "USD", Decimal("100000"), Decimal("99000"), Decimal("1000"),
            Decimal("0"), Decimal("100000"), 1, 0,
        ),
    )
    live = SimpleNamespace(instrument_id=100, generation=1)
    updated = SimpleNamespace(
        generation=2, baseline_candle_timestamp="2026-08-20T18:00:00+00:00"
    )
    with (
        patch("trading_simulator.dashboard._execution_summary", return_value={"pending": False}),
        patch("trading_simulator.dashboard.EtoroCredentials.from_environment"),
        patch("trading_simulator.dashboard.EtoroDemoReadOnlyClient", return_value=client),
        patch("trading_simulator.dashboard.EtoroLiveStateStore.load", return_value=live),
        patch("trading_simulator.dashboard.EtoroLiveStateStore.rebaseline_flat", return_value=updated) as rebaseline,
        patch("trading_simulator.dashboard._last_json", return_value={"candle_timestamp": "2026-08-20T18:00:00+00:00"}),
        patch("trading_simulator.dashboard._append_jsonl") as audit,
    ):
        result = actions.confirm_flat_and_rebaseline("btc", "operator")
    assert result["order_submitted"] is False
    rebaseline.assert_called_once()
    assert audit.call_args.args[1]["other_demo_positions_preserved"] == 1


def test_dashboard_never_offers_flat_rebaseline_for_open_live_position() -> None:
    directory = PROJECT / "outputs" / f".pytest-dashboard-open-{uuid4().hex}"
    directory.mkdir(parents=True)
    try:
        (directory / "xrp-readiness.jsonl").write_text(
            json.dumps({
                "candle_timestamp": "2026-08-20T19:00:00+00:00",
                "reason": "Demo portfolio and simulator position state do not reconcile",
                "decision": "buy",
                "ready": False,
            }) + "\n",
            encoding="utf-8",
        )
        (directory / "xrp-fifteen-minutes.live-state.json").write_text(
            json.dumps({"broker_position_id": 321, "instrument_id": 100003}),
            encoding="utf-8",
        )

        xrp = DashboardData(directory).snapshot().assets[3]

        assert xrp["holding_position"] is True
        assert xrp["can_flat_rebaseline"] is False
    finally:
        shutil.rmtree(directory)


def test_dashboard_demo_execution_requires_exact_arming_phrase() -> None:
    actions = DashboardActions(FIXTURES / "dashboard_valid", FIXTURES)

    with pytest.raises(EtoroDemoError, match="arming phrase does not match"):
        actions.execute_demo_intent(
            "btc", "intent", "operator", "WRONG", "100.00"
        )


def test_dashboard_resolves_rejected_close_by_read_only_verification() -> None:
    actions = DashboardActions(FIXTURES / "dashboard_valid", FIXTURES)
    ledger = SimpleNamespace(path=Path("ledger.jsonl"), record_rejection=Mock())
    client = SimpleNamespace(
        demo_pnl=lambda: {"clientPortfolio": {
            "positions": [{
                "positionID": 321,
                "instrumentID": 100003,
                "isBuy": True,
                "leverage": 1,
            }],
            "mirrors": [],
        }},
        demo_summary=lambda payload: EtoroDemoPortfolioSummary(
            "USD", Decimal("99000"), Decimal("99000"), Decimal("1000"),
            Decimal("0"), Decimal("100000"), 1, 0,
        ),
    )
    audited = SimpleNamespace(
        action="close-entire-long-position",
        request_path_template="/api/v1/trading/execution/demo/positions/321",
    )
    live = SimpleNamespace(broker_position_id=321, instrument_id=100003)
    with (
        patch("trading_simulator.dashboard.ExecutionLedger", return_value=ledger),
        patch("trading_simulator.dashboard._execution_summary", return_value={
            "pending": True, "status": "attempting", "intent_id": "close-1",
        }),
        patch("trading_simulator.dashboard.IntentAuditReader.load", return_value=audited),
        patch("trading_simulator.dashboard.EtoroLiveStateStore.load", return_value=live),
        patch("trading_simulator.dashboard.EtoroCredentials.from_environment"),
        patch("trading_simulator.dashboard.EtoroDemoReadOnlyClient", return_value=client),
    ):
        result = actions.resolve_rejected_close(
            "xrp", "close-1", "operator", "I_SAW_HTTP_400_REJECTION"
        )

    assert result["action"] == "rejected_close_resolved"
    assert result["position_still_open"] is True
    assert result["order_submitted"] is False
    ledger.record_rejection.assert_called_once()


def test_execution_summary_detects_pending_and_terminal_reconciliation() -> None:
    directory = PROJECT / "outputs" / f".pytest-dashboard-ledger-{uuid4().hex}"
    path = directory / "ledger.jsonl"
    directory.mkdir(parents=True)
    try:
        path.write_text(
            json.dumps({"intent_id": "intent-1", "status": "attempting"}) + "\n"
            + json.dumps({"intent_id": "intent-1", "status": "response_received"}) + "\n",
            encoding="utf-8",
        )
        pending = _execution_summary(path)
        assert pending == {
            "status": "response_received",
            "pending": True,
            "intent_id": "intent-1",
        }
        with path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps({"intent_id": "intent-1", "status": "position_reconciled"})
                + "\n"
            )
        assert _execution_summary(path)["pending"] is False
    finally:
        shutil.rmtree(directory)


def test_dashboard_reconciliation_reuses_read_only_cli_without_order() -> None:
    actions = DashboardActions(FIXTURES / "dashboard_valid", PROJECT)
    completed = SimpleNamespace(returncode=0, stdout="reconciled")
    with (
        patch(
            "trading_simulator.dashboard._execution_summary",
            return_value={
                "status": "response_received", "pending": True,
                "intent_id": "intent-1",
            },
        ),
        patch("trading_simulator.dashboard.subprocess.run", return_value=completed) as run,
        patch(
            "trading_simulator.dashboard.EtoroLiveStateStore.load",
            return_value=SimpleNamespace(broker_position_id=123),
        ),
    ):
        result = actions.reconcile_demo_execution("btc", "intent-1")

    assert result["action"] == "demo_execution_reconciled"
    assert result["position_open"] is True
    assert result["order_submitted"] is False
    assert result["order_retried"] is False
    command = run.call_args.args[0]
    assert "etoro-demo-reconcile-execution" in command
    assert "etoro-demo-execute-intent" not in command

