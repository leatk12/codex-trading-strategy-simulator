"""Small command-line entry point for educational project milestones."""

from __future__ import annotations

import argparse
import getpass
import json
import time
import webbrowser
from dataclasses import replace
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .analytics import PerformanceAnalyzer
from .audit import AuditExporter, AuditExportError
from .backtest import Backtest
from .config import load_asset_profile
from .domain import Action, Decision, MarketSnapshot, MarketState
from .dashboard import DashboardAuthenticator, hash_dashboard_password, serve_dashboard
from .experiments import (
    ExperimentCase,
    ExperimentError,
    OutOfSampleSplitter,
    ParameterExperiment,
)
from .etoro_demo import (
    EtoroCredentials,
    EtoroDemoError,
    EtoroDemoPortfolioSummary,
    EtoroDemoReadOnlyClient,
)
from .etoro_shadow import EtoroDryRunner, EtoroDryRunResult, RESOLUTIONS
from .etoro_live_state import EtoroLiveStateStore
from .etoro_reconciliation import reconcile_closed_long, reconcile_open_long
from .etoro_shadow_loop import EtoroShadowRecorder
from .etoro_intent import (
    EtoroIntentBuilder,
    IntentAuditWriter,
    IntentConstraints,
    ReadinessAuditWriter,
)
from .etoro_demo_execution import (
    ARMING_PHRASE,
    EtoroDemoExecutionClient,
    ExecutionLedger,
    IntentAuditReader,
)
from .shadow_control import (
    ShadowControlState,
    ShadowControlStore,
    load_latest_risk_event,
)
from .market_data import CsvMarketDataLoader, MarketDataError
from .market_states import MarketStateClassifier
from .portfolio import Portfolio
from .portfolio_risk import PortfolioRiskController, load_portfolio_risk_policy


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-sim")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect-data", help="validate and summarise an OHLCV CSV file"
    )
    inspect_parser.add_argument("csv_path", help="path to the OHLCV CSV file")
    inspect_parser.add_argument("--symbol", required=True, help="asset symbol")

    accounting_parser = subparsers.add_parser(
        "accounting-demo",
        help="buy at the first CSV close and sell at the last CSV close",
    )
    accounting_parser.add_argument("csv_path", help="path to the OHLCV CSV file")
    accounting_parser.add_argument(
        "--config", required=True, help="path to an asset-profile TOML file"
    )

    backtest_parser = subparsers.add_parser(
        "basic-backtest",
        help="run the Milestone 4 fixed-profit/re-entry strategy",
    )
    backtest_parser.add_argument("csv_path", help="path to the OHLCV CSV file")
    backtest_parser.add_argument(
        "--config", required=True, help="path to an asset-profile TOML file"
    )

    report_parser = subparsers.add_parser(
        "performance-report",
        help="run the strategy and print Milestone 9 analytics",
    )
    report_parser.add_argument("csv_path", help="path to the OHLCV CSV file")
    report_parser.add_argument(
        "--config", required=True, help="path to an asset-profile TOML file"
    )
    report_parser.add_argument(
        "--manual-approval-at",
        help="ISO-8601 timestamp that approves one active manual review",
    )
    backtest_parser.add_argument(
        "--manual-approval-at",
        help="ISO-8601 timestamp that approves one active manual review",
    )

    states_parser = subparsers.add_parser(
        "inspect-states",
        help="classify and explain market state for every CSV candle",
    )
    states_parser.add_argument("csv_path", help="path to the OHLCV CSV file")
    states_parser.add_argument(
        "--config", required=True, help="path to an asset-profile TOML file"
    )

    experiment_parser = subparsers.add_parser(
        "parameter-experiment",
        help="compare explicitly supplied parameter cases on development and holdout data",
    )
    experiment_parser.add_argument("csv_path", help="path to the OHLCV CSV file")
    experiment_parser.add_argument(
        "--config", required=True, help="path to an asset-profile TOML file"
    )
    experiment_parser.add_argument(
        "--parameter", required=True, help="AssetProfile parameter to vary"
    )
    experiment_parser.add_argument(
        "--values", nargs="+", required=True, help="manually selected parameter values"
    )
    experiment_parser.add_argument(
        "--versions",
        nargs="+",
        required=True,
        help="unique strategy version for each value, in matching order",
    )
    split_group = experiment_parser.add_mutually_exclusive_group(required=True)
    split_group.add_argument(
        "--development-fraction",
        type=Decimal,
        help="chronological fraction reserved for development (for example 0.70)",
    )
    split_group.add_argument(
        "--split-at",
        help="ISO-8601 start timestamp of the out-of-sample period",
    )

    export_parser = subparsers.add_parser(
        "export-backtest",
        help="run a backtest and persist its Milestone 11 audit bundle",
    )
    export_parser.add_argument("csv_path", help="path to the OHLCV CSV file")
    export_parser.add_argument(
        "--config", required=True, help="path to an asset-profile TOML file"
    )
    export_parser.add_argument(
        "--output-dir", required=True, help="new or non-conflicting audit directory"
    )
    export_parser.add_argument(
        "--manual-approval-at",
        help="ISO-8601 timestamp that approves one active manual review",
    )

    subparsers.add_parser(
        "etoro-demo-check",
        help="perform one read-only eToro Demo portfolio authentication check",
    )
    dry_run_parser = subparsers.add_parser(
        "etoro-dry-run",
        help="replay completed eToro candles and print a non-executable proposal",
    )
    dry_run_parser.add_argument(
        "--config", required=True, help="path to an asset-profile TOML file"
    )
    dry_run_parser.add_argument(
        "--symbol", required=True, help="exact eToro internal symbol (for example BTC)"
    )
    dry_run_parser.add_argument(
        "--resolution", required=True, choices=tuple(RESOLUTIONS)
    )
    dry_run_parser.add_argument("--candles", required=True, type=int)
    shadow_parser = subparsers.add_parser(
        "etoro-shadow-loop",
        help="continuously record read-only decisions for newly completed candles",
    )
    shadow_parser.add_argument(
        "--config", required=True, help="path to an asset-profile TOML file"
    )
    shadow_parser.add_argument("--symbol", required=True, help="exact eToro symbol")
    shadow_parser.add_argument(
        "--resolution", required=True, choices=tuple(RESOLUTIONS)
    )
    shadow_parser.add_argument("--candles", required=True, type=int)
    shadow_parser.add_argument(
        "--log", required=True, help="append-only JSONL decision log"
    )
    shadow_parser.add_argument(
        "--poll-seconds", type=int, default=60, help="poll interval (minimum 30)"
    )
    shadow_parser.add_argument(
        "--max-cycles",
        type=int,
        help="optional finite cycle count for supervised testing",
    )
    shadow_parser.add_argument(
        "--control", help="control JSON path (defaults beside the decision log)"
    )

    review_parser = subparsers.add_parser(
        "etoro-shadow-review", help="show the active shadow risk event and evidence"
    )
    review_parser.add_argument("--log", required=True, help="shadow JSONL log")
    review_parser.add_argument("--control", help="shadow control JSON path")

    approve_parser = subparsers.add_parser(
        "etoro-shadow-approve", help="approve one exact active shadow risk event"
    )
    approve_parser.add_argument("--log", required=True, help="shadow JSONL log")
    approve_parser.add_argument("--control", help="shadow control JSON path")
    approve_parser.add_argument("--event-id", required=True)
    approve_parser.add_argument("--approved-by", required=True)
    approve_parser.add_argument(
        "--acknowledge-risk",
        action="store_true",
        help="confirm the displayed event and evidence were reviewed",
    )

    kill_parser = subparsers.add_parser(
        "etoro-shadow-kill-switch", help="enable or disable the shadow kill switch"
    )
    kill_parser.add_argument("--log", required=True, help="shadow JSONL log")
    kill_parser.add_argument("--control", help="shadow control JSON path")
    kill_mode = kill_parser.add_mutually_exclusive_group(required=True)
    kill_mode.add_argument("--enable", action="store_true")
    kill_mode.add_argument("--disable", action="store_true")
    kill_parser.add_argument("--changed-by", required=True)
    kill_parser.add_argument("--reason", required=True)

    intent_parser = subparsers.add_parser(
        "etoro-demo-intent",
        help="reconcile Demo state and audit a non-executing order intent",
    )
    intent_parser.add_argument("--config", required=True)
    intent_parser.add_argument("--symbol", required=True)
    intent_parser.add_argument(
        "--resolution", required=True, choices=tuple(RESOLUTIONS)
    )
    intent_parser.add_argument("--candles", required=True, type=int)
    intent_parser.add_argument("--shadow-log", required=True)
    intent_parser.add_argument(
        "--live-state",
        help="broker-aligned live-state checkpoint (defaults beside shadow log)",
    )
    intent_parser.add_argument("--control")
    intent_parser.add_argument("--intent-log", required=True)
    intent_parser.add_argument(
        "--minimum-order-usd", required=True, type=Decimal
    )
    intent_parser.add_argument(
        "--amount-increment-usd", required=True, type=Decimal
    )
    intent_parser.add_argument(
        "--max-candle-age-minutes", type=int, default=90
    )
    intent_parser.add_argument(
        "--portfolio-risk-config", default="configs/portfolio_risk.toml"
    )
    intent_parser.add_argument(
        "--portfolio-risk-state",
        help="shared portfolio-risk JSON state (defaults beside shadow logs)",
    )

    readiness_parser = subparsers.add_parser(
        "etoro-readiness-loop",
        help="continuously audit Demo readiness once per completed candle",
    )
    for action in intent_parser._actions:
        if action.dest in {"help", "intent_log"}:
            continue
        readiness_parser._add_action(action)
    readiness_parser.add_argument("--intent-log", required=True)
    readiness_parser.add_argument(
        "--execution-ledger",
        help="execution ledger used to latch an unattempted intent",
    )
    readiness_parser.add_argument("--readiness-log", required=True)
    readiness_parser.add_argument("--poll-seconds", type=int, default=60)
    readiness_parser.add_argument("--max-cycles", type=int)
    readiness_parser.add_argument(
        "--max-consecutive-read-errors",
        type=int,
        default=5,
        help="retry this many consecutive transient read failures before halting",
    )
    readiness_report_parser = subparsers.add_parser(
        "etoro-readiness-report",
        help="summarise a continuous readiness JSONL audit",
    )
    readiness_report_parser.add_argument("--readiness-log", required=True)

    execute_parser = subparsers.add_parser(
        "etoro-demo-execute-intent",
        help="submit one revalidated, explicitly armed Demo long-buy intent",
    )
    for action in intent_parser._actions:
        if action.dest == "help":
            continue
        execute_parser._add_action(action)
    execute_parser.add_argument("--intent-id", required=True)
    execute_parser.add_argument("--execution-ledger", required=True)
    execute_parser.add_argument("--max-demo-order-usd", required=True, type=Decimal)
    execute_parser.add_argument("--arm-demo-execution", required=True)
    reconcile_parser = subparsers.add_parser(
        "etoro-demo-reconcile-execution",
        help="read Demo P&L until one submitted intent becomes an exact position",
    )
    reconcile_parser.add_argument("--intent-log", required=True)
    reconcile_parser.add_argument("--intent-id", required=True)
    reconcile_parser.add_argument("--execution-ledger", required=True)
    reconcile_parser.add_argument("--live-state", required=True)
    reconcile_parser.add_argument("--poll-seconds", type=int, default=5)
    reconcile_parser.add_argument("--timeout-seconds", type=int, default=60)
    reconcile_parser.add_argument(
        "--portfolio-risk-config", default="configs/portfolio_risk.toml"
    )
    reconcile_parser.add_argument(
        "--portfolio-risk-state",
        default="outputs/shadow/portfolio-risk-state.json",
    )
    dashboard_parser = subparsers.add_parser(
        "dashboard", help="serve the local read-only readiness dashboard"
    )
    dashboard_parser.add_argument(
        "--data-dir", default="outputs/shadow", help="directory containing audit logs"
    )
    dashboard_parser.add_argument("--port", type=int, default=8765)
    dashboard_parser.add_argument("--no-browser", action="store_true")
    subparsers.add_parser(
        "dashboard-password-hash",
        help="securely prompt for a dashboard password and print its salted hash",
    )
    synthetic_parser = subparsers.add_parser(
        "test-intent-pipeline",
        help="run an offline synthetic BUY through the real intent validator",
    )
    synthetic_parser.add_argument("--config", required=True)
    synthetic_parser.add_argument("--output-dir", required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    options = parser.parse_args(arguments)

    if options.command == "test-intent-pipeline":
        try:
            profile = load_asset_profile(options.config)
            output_dir = Path(options.output_dir)
            now = datetime.now(UTC)
            candle_start = (
                now.replace(minute=0, second=0, microsecond=0)
                - timedelta(hours=1)
            )
            candle = MarketSnapshot(
                symbol=profile.symbol,
                timestamp=candle_start,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1000"),
            )
            budget = min(profile.initial_investment, profile.maximum_position_size)
            decision = Decision(
                action=Action.BUY,
                state=MarketState.NORMAL,
                timestamp=candle.timestamp,
                price=candle.close,
                reason="Offline synthetic test of the intent-writing pipeline.",
                facts={"synthetic_test": "true"},
                cash_budget=budget,
            )
            shadow = EtoroDryRunResult(
                strategy_version=profile.strategy_version,
                requested_symbol=profile.symbol.removesuffix("-USD"),
                profile_symbol=profile.symbol,
                resolution=RESOLUTIONS["one-hour"],
                completed_candle_count=1,
                latest_candle=candle,
                latest_decision=decision,
                proposed_action="buy",
                proposed_cash_budget=budget,
                instrument_id=999999,
                simulated_position_open=False,
            )
            summary = EtoroDemoPortfolioSummary(
                currency="USD",
                credit=Decimal("100000"),
                available_cash=Decimal("100000"),
                total_invested=Decimal("0"),
                unrealized_profit_loss=Decimal("0"),
                equity=Decimal("100000"),
                open_position_count=0,
                pending_order_count=0,
            )
            payload = {
                "clientPortfolio": {
                    "positions": [],
                    "orders": [],
                    "ordersForOpen": [],
                    "mirrors": [],
                }
            }
            readiness = EtoroIntentBuilder(
                now=now, environment="synthetic_test"
            ).build(
                profile,
                shadow,
                summary,
                payload,
                ShadowControlState(),
                IntentConstraints(
                    minimum_order_usd=Decimal("10.00"),
                    amount_increment_usd=Decimal("0.01"),
                    maximum_candle_age=timedelta(minutes=90),
                ),
            )
            intent_path = output_dir / "synthetic-intents.jsonl"
            readiness_path = output_dir / "synthetic-readiness.jsonl"
            written = IntentAuditWriter(intent_path).append(readiness)
            ReadinessAuditWriter(readiness_path).append(
                shadow, readiness, summary, observed_at=now
            )
        except (EtoroDemoError, ValueError) as error:
            parser.error(str(error))
        print("Offline synthetic intent-pipeline test")
        print(f"ready={str(readiness.ready).lower()}")
        print(f"decision={decision.action.value}")
        print(f"intent_written={str(written).lower()}")
        print("submitted=NO")
        print("environment=synthetic_test")
        print("execution_eligible=false")
        print("network_access=NONE")
        print("leverage=1x (no borrowing)")
        print(f"Intent audit:       {intent_path.resolve()}")
        print(f"Readiness audit:    {readiness_path.resolve()}")
    elif options.command == "etoro-demo-reconcile-execution":
        if options.poll_seconds < 2:
            parser.error("--poll-seconds must be at least 2")
        if options.timeout_seconds < options.poll_seconds:
            parser.error("--timeout-seconds must be at least --poll-seconds")
        try:
            audited = IntentAuditReader(options.intent_log).load(options.intent_id)
            ledger = ExecutionLedger(options.execution_ledger)
            ledger.assert_submitted(audited.intent_id)
            live_store = EtoroLiveStateStore(options.live_state)
            state = live_store.load()
            if state is None:
                raise EtoroDemoError("live-state checkpoint does not exist")
            is_close = audited.action == "close-entire-long-position"
            if is_close:
                closed_position_id = int(
                    audited.request_path_template.rsplit("/", 1)[-1]
                )
                known_ids = state.broker_position_ids or (
                    (() if state.broker_position_id is None else (state.broker_position_id,))
                )
                if closed_position_id not in known_ids:
                    raise EtoroDemoError(
                        "close intent does not belong to this live-state checkpoint"
                    )
            else:
                expected_instrument = int(audited.request_body.get("instrumentId", 0))
                if state.instrument_id != expected_instrument:
                    raise EtoroDemoError("intent does not belong to this live-state checkpoint")
            client = EtoroDemoReadOnlyClient(EtoroCredentials.from_environment())
            deadline = time.monotonic() + options.timeout_seconds
            position = None
            while position is None:
                payload = client.demo_pnl()
                position = (
                    reconcile_closed_long(
                        audited,
                        payload,
                        expected_instrument_id=state.instrument_id,
                        allowed_remaining_position_ids=tuple(
                            item for item in state.broker_position_ids
                            if item != closed_position_id
                        ),
                    )
                    if is_close
                    else reconcile_open_long(
                        audited,
                        payload,
                        existing_position_ids=state.broker_position_ids,
                    )
                )
                if position is not None:
                    break
                if time.monotonic() >= deadline:
                    raise EtoroDemoError(
                        "reconciliation timed out with no confirmed position; "
                        "do not resubmit the order"
                    )
                time.sleep(options.poll_seconds)
            fields = position.audit_fields()
            if is_close:
                ledger.record_close_reconciliation(audited.intent_id, fields)
                live_store.record_closed_position(
                    intent_id=audited.intent_id,
                    position_id=position.position_id,
                    pnl_payload=payload,
                )
            else:
                ledger.record_reconciliation(audited.intent_id, fields)
                live_store.record_open_position(
                    intent_id=audited.intent_id,
                    position_id=position.position_id,
                    amount_usd=str(position.amount_usd),
                    units=str(position.units),
                    open_rate=str(position.open_rate),
                    fees_usd=(
                        None if position.fees_usd is None else str(position.fees_usd)
                    ),
                )
                if Path(options.portfolio_risk_state).exists():
                    PortfolioRiskController(
                        load_portfolio_risk_policy(options.portfolio_risk_config),
                        options.portfolio_risk_state,
                    ).release_reservation(
                        audited.intent_id,
                        changed_by="reconciliation-service",
                        reason="Demo buy position reconciled",
                    )
        except (EtoroDemoError, ValueError, TypeError) as error:
            parser.error(str(error))
        print("eToro Demo execution reconciled")
        print(f"Intent ID:        {audited.intent_id}")
        print(f"Position ID:      {position.position_id}")
        if is_close:
            print("Result:           full position closed")
        else:
            print(f"Amount:           {position.amount_usd} USD")
            print(f"Units:            {position.units}")
            print(f"Fill/open rate:   {position.open_rate}")
            print(
                "Broker fees:      "
                + (
                    "not exposed by Demo P&L"
                    if position.fees_usd is None
                    else f"{position.fees_usd} USD"
                )
            )
            print("Direction:        long")
        print("Leverage:         1x (no borrowing)")
        print("Order retry:      BLOCKED")
        print(f"Live state:       {live_store.path.resolve()}")
    elif options.command == "dashboard-password-hash":
        password = getpass.getpass("New dashboard password (12+ characters): ")
        confirmation = getpass.getpass("Confirm dashboard password: ")
        if password != confirmation:
            parser.error("dashboard passwords do not match")
        try:
            print(hash_dashboard_password(password))
        except ValueError as error:
            parser.error(str(error))
    elif options.command == "dashboard":
        if not 1 <= options.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        try:
            dashboard_auth = DashboardAuthenticator.from_environment()
        except ValueError as error:
            parser.error(f"could not start dashboard: {error}")
        url = f"http://127.0.0.1:{options.port}/"
        print("Codex Trading Simulator dashboard")
        print(f"URL:              {url}")
        print(f"Audit directory:  {Path(options.data_dir).resolve()}")
        print("Network exposure: localhost only")
        print("Order execution:  REAL BLOCKED; explicitly armed Demo only")
        print("Leverage:         1x (no borrowing)")
        print("Press Ctrl+C to stop.\n")
        if not options.no_browser:
            webbrowser.open(url)
        try:
            serve_dashboard(
                options.data_dir, options.port, Path.cwd(), dashboard_auth
            )
        except (OSError, ValueError) as error:
            parser.error(f"could not start dashboard: {error}")
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
    elif options.command == "inspect-data":
        try:
            data = CsvMarketDataLoader(options.csv_path, options.symbol).load()
        except MarketDataError as error:
            parser.error(str(error))

        first = data[0]
        last = data[-1]
        print(f"Validated {len(data)} candles for {data.symbol}")
        print(f"Range: {data.start.isoformat()} to {data.end.isoformat()}")
        print(
            f"First OHLCV: {first.open}, {first.high}, {first.low}, "
            f"{first.close}, {first.volume}"
        )
        print(
            f"Last OHLCV:  {last.open}, {last.high}, {last.low}, "
            f"{last.close}, {last.volume}"
        )
    elif options.command == "accounting-demo":
        profile = load_asset_profile(options.config)
        data = CsvMarketDataLoader(options.csv_path, profile.symbol).load()
        portfolio = Portfolio(profile)
        purchase = portfolio.buy(
            profile.initial_investment,
            data[0].close,
            data[0].timestamp,
            "Milestone 3 demo: buy at first candle close",
        )
        cost_basis = portfolio.invested_capital
        sale = portfolio.sell_all(
            data[-1].close,
            data[-1].timestamp,
            "Milestone 3 demo: sell at last candle close",
        )
        print(f"Strategy version: {profile.strategy_version}")
        print(f"Starting cash:    {portfolio.starting_capital:.2f}")
        print(f"Buy midpoint:     {purchase.market_price:.2f}")
        print(f"Buy execution:    {purchase.simulated_price:.2f}")
        print(f"Quantity:         {purchase.quantity:.12f}")
        print(f"All-in cost basis:{cost_basis:>12.2f}")
        print(f"Sell midpoint:    {sale.market_price:.2f}")
        print(f"Sell execution:   {sale.simulated_price:.2f}")
        print(f"Ending cash:      {portfolio.cash:.2f}")
        print(f"Realised profit:  {portfolio.realised_profit:.2f}")
        print(f"Trading costs:    {portfolio.total_trading_costs:.2f}")
    elif options.command == "basic-backtest":
        profile = load_asset_profile(options.config)
        data = CsvMarketDataLoader(options.csv_path, profile.symbol).load()
        approval_at = (
            None
            if options.manual_approval_at is None
            else _parse_timestamp(options.manual_approval_at)
        )
        result = Backtest(
            profile, data, manual_approval_at=approval_at
        ).run()

        print(f"Strategy version: {result.strategy_version}")
        print("\nDecision log")
        for decision in result.decisions:
            print(
                f"{decision.timestamp.isoformat()} | "
                f"{decision.action.value.upper():<4} | "
                f"state={decision.state.value:<18} | "
                f"price={decision.price} | {decision.reason}"
            )
            for name, value in decision.facts.items():
                print(f"    {name}: {value}")
            if decision.cash_budget is not None:
                print(f"    cash_budget: {decision.cash_budget}")
                print(
                    f"    profit_reinvestment: {decision.profit_reinvestment}"
                )
            if decision.reentry_stage is not None:
                print(f"    requested_reentry_stage: {decision.reentry_stage}")

        print("\nSummary")
        print(f"Starting capital: {result.starting_capital:.2f}")
        print(f"Ending capital:   {result.ending_capital:.2f}")
        print(f"Total return:     {result.metrics['total_return_rate']:.2%}")
        print(f"Realised profit:  {result.metrics['realised_profit']:.2f}")
        print(f"Unrealised profit:{result.metrics['unrealised_profit']:>10.2f}")
        print(f"Trading costs:    {result.metrics['trading_costs']:.2f}")
        print(f"Profit reinvested:{result.metrics['profit_reinvested']:>10.2f}")
        print(f"Re-entry buys:    {result.metrics['reentry_allocations']:.0f}")
        print(f"Manual reviews:   {result.metrics['manual_review_events']:.0f}")
        print(f"Manual approvals: {result.metrics['manual_approvals']:.0f}")
        print(f"Trades:           {len(result.trades)}")
    elif options.command == "performance-report":
        profile = load_asset_profile(options.config)
        data = CsvMarketDataLoader(options.csv_path, profile.symbol).load()
        approval_at = (
            None
            if options.manual_approval_at is None
            else _parse_timestamp(options.manual_approval_at)
        )
        result = Backtest(
            profile, data, manual_approval_at=approval_at
        ).run()
        report = PerformanceAnalyzer(profile).analyze(result, data)

        print(f"Strategy version:             {report.strategy_version}")
        print(f"Starting capital:             {report.starting_capital:.2f}")
        print(f"Ending capital:               {report.ending_capital:.2f}")
        print(f"Total return:                 {report.total_return_rate:.2%}")
        print(f"Realised profit:              {report.realised_profit:.2f}")
        print(f"Unrealised profit:            {report.unrealised_profit:.2f}")
        print(f"Trades (orders):              {report.trade_count}")
        print(f"Completed trades (sales):     {report.completed_trade_count}")
        print(f"Profitable / losing / even:   {report.profitable_trades} / {report.losing_trades} / {report.break_even_trades}")
        print(f"Win rate:                     {report.win_rate:.2%}")
        print(f"Average completed P&L:        {report.average_profit_per_completed_trade:.2f}")
        print(f"Average winner:              {report.average_winning_trade:>10.2f}")
        print(f"Average loss:                {report.average_loss:>10.2f}")
        print(f"Maximum drawdown:             {report.maximum_drawdown:.2%}")
        print(f"Fees paid:                    {report.fees_paid:.2f}")
        print(f"Spread cost:                  {report.spread_cost_paid:.2f}")
        print(f"Slippage cost:                {report.slippage_cost_paid:.2f}")
        print(f"Total trading costs:          {report.total_trading_costs:.2f}")
        print(f"Time invested:                {_format_duration(report.invested_duration)} ({report.invested_time_rate:.2%})")
        print(f"Time in cash:                 {_format_duration(report.cash_duration)} ({report.cash_time_rate:.2%})")
        print(f"Largest all-in position:      {report.largest_position:.2f}")
        print(f"Manual-review events:         {report.manual_review_events}")
        print(f"Buy-and-hold ending capital:  {report.buy_and_hold_ending_capital:.2f}")
        print(f"Buy-and-hold return:          {report.buy_and_hold_return_rate:.2%}")
        print(f"Excess vs buy-and-hold:       {report.excess_return_vs_buy_and_hold:.2%}")
        print(f"Period return volatility:     {report.period_return_volatility:.6f}")
        print(
            "Unannualised period Sharpe:  "
            + (
                "not_available"
                if report.period_sharpe_ratio is None
                else f"{report.period_sharpe_ratio:.6f}"
            )
        )
        print(f"Leverage allowed:             {str(False).lower()}")
        print(f"Leverage used:                {report.leverage_used:.2f}")
    elif options.command == "inspect-states":
        profile = load_asset_profile(options.config)
        data = CsvMarketDataLoader(options.csv_path, profile.symbol).load()
        classifier = MarketStateClassifier(profile)
        history = []
        previous_state = MarketState.NORMAL
        for snapshot in data:
            history.append(snapshot)
            assessment = classifier.classify(history, previous_state)
            print(
                f"{snapshot.timestamp.isoformat()} | close={snapshot.close:<6} | "
                f"{assessment.transition}"
            )
            print(f"    Reason: {assessment.reason}")
            for name, value in assessment.facts.items():
                print(f"    {name}: {value}")
            previous_state = assessment.state
    elif options.command == "parameter-experiment":
        profile = load_asset_profile(options.config)
        data = CsvMarketDataLoader(options.csv_path, profile.symbol).load()
        if len(options.values) != len(options.versions):
            parser.error("--values and --versions must contain the same number of items")
        try:
            values = tuple(
                _parse_experiment_value(profile, options.parameter, value)
                for value in options.values
            )
            cases = tuple(
                ExperimentCase(
                    label=f"{options.parameter}={value}",
                    strategy_version=version,
                    overrides={options.parameter: value},
                )
                for value, version in zip(values, options.versions, strict=True)
            )
            split = (
                OutOfSampleSplitter.by_fraction(data, options.development_fraction)
                if options.development_fraction is not None
                else OutOfSampleSplitter.by_timestamp(
                    data, _parse_timestamp(options.split_at, "split timestamp")
                )
            )
            comparison = ParameterExperiment(profile).run(split, cases)
        except (ExperimentError, argparse.ArgumentTypeError) as error:
            parser.error(str(error))

        print(f"Base strategy version: {profile.strategy_version}")
        print(
            "Development:          "
            f"{split.development.start.isoformat()} to {split.development.end.isoformat()} "
            f"({len(split.development)} candles)"
        )
        print(
            "Out of sample:        "
            f"{split.out_of_sample.start.isoformat()} to {split.out_of_sample.end.isoformat()} "
            f"({len(split.out_of_sample)} candles)"
        )
        print("\nCases (supplied order; not ranked)")
        print(
            "Version              Value          Dev return  Dev drawdown  Dev excess  "
            "OOS return  OOS drawdown  OOS excess"
        )
        for outcome in comparison.outcomes:
            development = outcome.development_report
            holdout = outcome.out_of_sample_report
            value = outcome.overrides[options.parameter]
            print(
                f"{outcome.strategy_version:<20} {str(value):<14} "
                f"{development.total_return_rate:>10.2%} "
                f"{development.maximum_drawdown:>13.2%} "
                f"{development.excess_return_vs_buy_and_hold:>10.2%} "
                f"{holdout.total_return_rate:>11.2%} "
                f"{holdout.maximum_drawdown:>13.2%} "
                f"{holdout.excess_return_vs_buy_and_hold:>10.2%}"
            )
        print(f"\nCaution: {comparison.caution}")
        print("No winner was selected and the base configuration was not changed.")
    elif options.command == "export-backtest":
        profile = load_asset_profile(options.config)
        data = CsvMarketDataLoader(options.csv_path, profile.symbol).load()
        try:
            approval_at = (
                None
                if options.manual_approval_at is None
                else _parse_timestamp(options.manual_approval_at)
            )
            result = Backtest(profile, data, manual_approval_at=approval_at).run()
            report = PerformanceAnalyzer(profile).analyze(result, data)
            paths = AuditExporter().export(result, report, options.output_dir)
        except (argparse.ArgumentTypeError, AuditExportError) as error:
            parser.error(str(error))
        print(f"Exported Milestone 11 audit bundle for {result.strategy_version}")
        for path in paths:
            print(f"  {path.resolve()}")
        print("Leverage allowed: false")
    elif options.command == "etoro-demo-check":
        try:
            client = EtoroDemoReadOnlyClient(EtoroCredentials.from_environment())
            summary = client.demo_summary()
        except EtoroDemoError as error:
            parser.error(str(error))
        print("Authenticated against the eToro Demo P&L endpoint.")
        print("\nSanitised virtual portfolio summary")
        print(f"Currency:              {summary.currency}")
        print(f"Credit:                {summary.credit:.2f}")
        print(f"Available cash:        {summary.available_cash:.2f}")
        print(f"Total invested:        {summary.total_invested:.2f}")
        print(f"Unrealised P&L:        {summary.unrealized_profit_loss:.2f}")
        print(f"Calculated equity:     {summary.equity:.2f}")
        print(f"Open positions:        {summary.open_position_count}")
        print(f"Pending manual orders: {summary.pending_order_count}")
        print()
        print("Adapter mode: read-only")
        print("Order execution: blocked")
        print("Real-account access: blocked")
        print("Leverage allowed by simulator: false")
    elif options.command == "etoro-dry-run":
        try:
            profile = load_asset_profile(options.config)
            client = EtoroDemoReadOnlyClient(EtoroCredentials.from_environment())
            dry_run = EtoroDryRunner(client).run(
                profile,
                symbol=options.symbol,
                resolution=options.resolution,
                candle_count=options.candles,
            )
        except (EtoroDemoError, ValueError) as error:
            parser.error(str(error))
        print("eToro Demo one-shot strategy dry run")
        print(f"Instrument:       {dry_run.requested_symbol} (profile {dry_run.profile_symbol})")
        print(f"Resolution:       {dry_run.resolution.api_name}")
        print(f"Completed candles:{dry_run.completed_candle_count:>7}")
        print(f"Latest candle:    {dry_run.latest_candle.timestamp.isoformat()}")
        print(f"Market state:     {dry_run.latest_decision.state.value}")
        print(f"Decision:         {dry_run.latest_decision.action.value}")
        print(f"Proposed action:  {dry_run.proposed_action}")
        if dry_run.proposed_cash_budget is not None:
            print(f"Proposed budget:  {dry_run.proposed_cash_budget:.2f}")
        print(f"Reason:           {dry_run.latest_decision.reason}")
        print("Leverage:         1x (no borrowing)")
        print("Order submitted:  NO")
        print("Adapter mode:     read-only")
    elif options.command == "etoro-shadow-loop":
        if options.poll_seconds < 30:
            parser.error("--poll-seconds must be at least 30")
        if options.max_cycles is not None and options.max_cycles < 1:
            parser.error("--max-cycles must be at least 1")
        if options.max_consecutive_read_errors < 1:
            parser.error("--max-consecutive-read-errors must be at least 1")
        try:
            profile = load_asset_profile(options.config)
            client = EtoroDemoReadOnlyClient(EtoroCredentials.from_environment())
            recorder = EtoroShadowRecorder(options.log)
            control_path = options.control or _default_control_path(options.log)
            control_store = ShadowControlStore(
                control_path, event_log_path=options.log
            )
            control_store.load()
        except (EtoroDemoError, ValueError) as error:
            parser.error(str(error))

        print("eToro Demo continuous shadow mode")
        print(f"Instrument:       {options.symbol.upper()} (profile {profile.symbol})")
        print(f"Resolution:       {RESOLUTIONS[options.resolution].api_name}")
        print(f"Decision log:     {recorder.path.resolve()}")
        print(f"Control state:    {control_store.path.resolve()}")
        print(f"Polling:          every {options.poll_seconds} seconds")
        print("Order execution:  BLOCKED")
        print("Leverage:         1x (no borrowing)")
        print("Press Ctrl+C to stop.\n")
        cycles = 0
        try:
            while True:
                try:
                    runner = EtoroDryRunner(client)
                    control = control_store.load()
                    shadow = runner.run(
                        profile,
                        symbol=options.symbol,
                        resolution=options.resolution,
                        candle_count=options.candles,
                        manual_approval_times=control.approval_times,
                    )
                    if control.kill_switch:
                        decision = shadow.latest_decision
                        shadow = replace(
                            shadow,
                            latest_decision=Decision(
                                action=Action.SUSPEND_AUTOMATIC_BUYING,
                                state=MarketState.MANUAL_REVIEW,
                                timestamp=decision.timestamp,
                                price=decision.price,
                                reason=(
                                    "Local kill switch is enabled; all proposed "
                                    "automatic action is blocked."
                                ),
                                facts={
                                    **decision.facts,
                                    "local_kill_switch": "true",
                                },
                            ),
                            proposed_action="none",
                            proposed_cash_budget=None,
                        )
                    outcome = recorder.record(shadow)
                except EtoroDemoError as error:
                    print(f"Shadow cycle failed safely: {error}")
                else:
                    status = "RECORDED" if outcome.recorded else "already recorded"
                    print(
                        f"{outcome.candle_timestamp.isoformat()} | {status} | "
                        f"state={shadow.latest_decision.state.value} | "
                        f"decision={shadow.latest_decision.action.value} | "
                        "submitted=NO"
                    )
                cycles += 1
                if options.max_cycles is not None and cycles >= options.max_cycles:
                    break
                time.sleep(options.poll_seconds)
        except KeyboardInterrupt:
            print("\nShadow mode stopped by user. No orders were submitted.")
    elif options.command == "etoro-shadow-review":
        try:
            event = load_latest_risk_event(options.log)
            store = ShadowControlStore(
                options.control or _default_control_path(options.log),
                event_log_path=options.log,
            )
            control = store.load()
        except EtoroDemoError as error:
            parser.error(str(error))
        print("Active shadow manual-review event")
        print(f"Event ID:          {event.event_id}")
        print(f"Triggered at:      {event.triggered_at.isoformat()}")
        print("Reasons:")
        for reason in event.reasons:
            print(f"  - {reason}")
        print("Evidence:")
        for name, value in sorted(event.evidence.items()):
            print(f"  {name}: {value}")
        print(f"Kill switch:       {'ON' if control.kill_switch else 'off'}")
        approval_matches = any(
            approval.event_id == event.event_id
            for approval in control.approvals
        )
        print(f"Event approved:    {str(approval_matches).lower()}")
        print("Order execution:   BLOCKED (shadow mode)")
    elif options.command == "etoro-shadow-approve":
        if not options.acknowledge_risk:
            parser.error("--acknowledge-risk is required after reviewing the event")
        try:
            event = load_latest_risk_event(options.log)
            if options.event_id != event.event_id:
                raise EtoroDemoError(
                    "--event-id does not match the currently active risk event"
                )
            store = ShadowControlStore(
                options.control or _default_control_path(options.log),
                event_log_path=options.log,
            )
            state = store.approve(event, approved_by=options.approved_by)
        except EtoroDemoError as error:
            parser.error(str(error))
        print(f"Approved shadow risk event: {event.event_id}")
        print(f"Approved at:               {state.approval.approved_at.isoformat()}")
        print(f"Approved by:               {state.approval.approved_by}")
        print("Scope:                     this exact event only")
        print("Order execution:           BLOCKED (shadow mode)")
    elif options.command == "etoro-shadow-kill-switch":
        try:
            store = ShadowControlStore(
                options.control or _default_control_path(options.log),
                event_log_path=options.log,
            )
            state = store.set_kill_switch(
                options.enable,
                changed_by=options.changed_by,
                reason=options.reason,
            )
        except EtoroDemoError as error:
            parser.error(str(error))
        print(f"Shadow kill switch: {'ENABLED' if state.kill_switch else 'disabled'}")
        print(f"Changed at:         {state.changed_at.isoformat()}")
        print(f"Changed by:         {state.changed_by}")
        print(f"Reason:             {state.kill_switch_reason}")
        print("Order execution:    BLOCKED (shadow mode)")
    elif options.command == "etoro-demo-intent":
        try:
            profile = load_asset_profile(options.config)
            credentials = EtoroCredentials.from_environment()
            client = EtoroDemoReadOnlyClient(credentials)
            control_store = ShadowControlStore(
                options.control
                or _default_control_path(options.shadow_log),
                event_log_path=options.shadow_log,
            )
            control = control_store.load()
            pnl_payload = client.demo_pnl()
            summary = client.demo_summary(pnl_payload)
            shadow, live_store = _broker_aligned_shadow(
                client, profile, options, control.approval_times, summary, pnl_payload
            )
            constraints = IntentConstraints(
                minimum_order_usd=options.minimum_order_usd,
                amount_increment_usd=options.amount_increment_usd,
                maximum_candle_age=timedelta(
                    minutes=options.max_candle_age_minutes
                ),
                portfolio_risk_controller=_portfolio_risk_controller(options),
            )
            readiness = EtoroIntentBuilder().build(
                profile,
                shadow,
                summary,
                pnl_payload,
                control,
                _live_intent_constraints(constraints, live_store),
            )
            written = IntentAuditWriter(options.intent_log).append(readiness)
        except (EtoroDemoError, ValueError) as error:
            parser.error(str(error))
        print("eToro Demo execution-readiness check")
        print(f"Candle:           {shadow.latest_candle.timestamp.isoformat()}")
        print(f"Decision:         {shadow.latest_decision.action.value}")
        print(f"Portfolio cash:   {summary.available_cash:.2f} USD")
        print(f"Open positions:   {summary.open_position_count}")
        print(f"Pending orders:   {summary.pending_order_count}")
        print(f"Ready:            {str(readiness.ready).lower()}")
        print(f"Reason:           {readiness.reason}")
        if readiness.intent is not None:
            print(f"Intent ID:        {readiness.intent.intent_id}")
            print(f"Intent audit:     {Path(options.intent_log).resolve()}")
            print(f"New record:       {str(written).lower()}")
        print("Leverage:         1x (no borrowing)")
        print("Order submitted:  NO")
        print("Adapter mode:     read-only")
        print(f"Live state:       {live_store.path.resolve()}")
    elif options.command == "etoro-readiness-loop":
        if options.poll_seconds < 30:
            parser.error("--poll-seconds must be at least 30")
        if options.max_cycles is not None and options.max_cycles < 1:
            parser.error("--max-cycles must be at least 1")
        try:
            profile = load_asset_profile(options.config)
            client = EtoroDemoReadOnlyClient(EtoroCredentials.from_environment())
            control_store = ShadowControlStore(
                options.control
                or _default_control_path(options.shadow_log),
                event_log_path=options.shadow_log,
            )
            constraints = IntentConstraints(
                minimum_order_usd=options.minimum_order_usd,
                amount_increment_usd=options.amount_increment_usd,
                maximum_candle_age=timedelta(
                    minutes=options.max_candle_age_minutes
                ),
                portfolio_risk_controller=_portfolio_risk_controller(options),
            )
            readiness_writer = ReadinessAuditWriter(options.readiness_log)
            intent_writer = IntentAuditWriter(options.intent_log)
            shadow_writer = EtoroShadowRecorder(options.shadow_log)
            live_store = EtoroLiveStateStore(
                options.live_state or _default_live_state_path(options.shadow_log)
            )
        except (EtoroDemoError, ValueError) as error:
            parser.error(str(error))
        print("eToro Demo continuous execution-readiness monitor")
        print(f"Readiness log:    {readiness_writer.path.resolve()}")
        print(f"Intent log:       {intent_writer.path.resolve()}")
        print(f"Live state:       {live_store.path.resolve()}")
        print(f"Polling:          every {options.poll_seconds} seconds")
        print(
            "Failure policy:   halt on safety/reconciliation failure; "
            f"retry {options.max_consecutive_read_errors} transient read errors"
        )
        print("Order execution:  BLOCKED")
        print("Leverage:         1x (no borrowing)")
        print("Press Ctrl+C to stop.\n")
        cycles = 0
        consecutive_read_errors = 0
        try:
            while True:
                try:
                    control = control_store.load()
                    pnl_payload = client.demo_pnl()
                    summary = client.demo_summary(pnl_payload)
                    shadow, live_store = _broker_aligned_shadow(
                        client,
                        profile,
                        options,
                        control.approval_times,
                        summary,
                        pnl_payload,
                    )
                    shadow_writer.record(shadow)
                    latched_intent_id = _latest_unresolved_intent_id(
                        intent_writer.path,
                        (
                            Path(options.execution_ledger)
                            if options.execution_ledger
                            else intent_writer.path.with_name(
                                intent_writer.path.name.replace("-intents", "-execution")
                            )
                        ),
                        live_store,
                    )
                    already_evaluated = readiness_writer.has_candle(
                        shadow.strategy_version,
                        shadow.requested_symbol,
                        shadow.latest_candle.timestamp,
                    )
                    if control.kill_switch:
                        print("HALTED safely: local kill switch is enabled")
                        break
                    if latched_intent_id is not None:
                        print(
                            f"{shadow.latest_candle.timestamp.isoformat()} | "
                            f"monitoring | intent_pending_approval={latched_intent_id} | "
                            "submitted=NO"
                        )
                    if already_evaluated:
                        print(
                            f"{shadow.latest_candle.timestamp.isoformat()} | "
                            "already evaluated | submitted=NO"
                        )
                    elif latched_intent_id is None:
                        readiness = EtoroIntentBuilder().build(
                            profile,
                            shadow,
                            summary,
                            pnl_payload,
                            control,
                            _live_intent_constraints(constraints, live_store),
                        )
                        readiness_writer.append(shadow, readiness, summary)
                        intent_written = intent_writer.append(readiness)
                        print(
                            f"{shadow.latest_candle.timestamp.isoformat()} | "
                            f"ready={str(readiness.ready).lower()} | "
                            f"decision={shadow.latest_decision.action.value} | "
                            f"intent_written={str(intent_written).lower()} | "
                            "submitted=NO"
                        )
                        print(f"    {readiness.reason}")
                        if readiness.halt_monitor:
                            print("HALTED safely; operator review is required.")
                            break
                    consecutive_read_errors = 0
                except EtoroDemoError as error:
                    if (
                        _is_transient_etoro_read_error(error)
                        and consecutive_read_errors
                        < options.max_consecutive_read_errors
                    ):
                        consecutive_read_errors += 1
                        print(
                            "Transient eToro read failure "
                            f"({consecutive_read_errors}/"
                            f"{options.max_consecutive_read_errors}): {error}"
                        )
                        print("    Retrying safely; no order was submitted.")
                        cycles += 1
                        if (
                            options.max_cycles is not None
                            and cycles >= options.max_cycles
                        ):
                            break
                        time.sleep(options.poll_seconds)
                        continue
                    print(f"HALTED safely: {error}")
                    break
                cycles += 1
                if options.max_cycles is not None and cycles >= options.max_cycles:
                    break
                time.sleep(options.poll_seconds)
        except KeyboardInterrupt:
            print("\nReadiness monitor stopped. No orders were submitted.")
    elif options.command == "etoro-readiness-report":
        try:
            report = ReadinessAuditWriter(options.readiness_log).report()
        except EtoroDemoError as error:
            parser.error(str(error))
        print("eToro Demo readiness report")
        print(f"Evaluations:       {report.evaluations}")
        print(f"Ready intents:     {report.ready}")
        print(f"Rejected/no-order: {report.rejected}")
        print(f"Halting failures:  {report.halting_failures}")
        print(
            "First candle:      "
            + ("not_available" if report.first_candle is None else report.first_candle.isoformat())
        )
        print(
            "Last candle:       "
            + ("not_available" if report.last_candle is None else report.last_candle.isoformat())
        )
        print("Reasons:")
        for reason, count in report.reason_counts.items():
            print(f"  {count:>5}  {reason}")
        print("Orders submitted:  0")
    elif options.command == "etoro-demo-execute-intent":
        if options.arm_demo_execution != ARMING_PHRASE:
            parser.error(
                "--arm-demo-execution must exactly equal " + ARMING_PHRASE
            )
        if options.max_demo_order_usd <= 0:
            parser.error("--max-demo-order-usd must be positive")
        try:
            profile = load_asset_profile(options.config)
            audited = IntentAuditReader(options.intent_log).load(options.intent_id)
            if audited.strategy_version != profile.strategy_version:
                raise EtoroDemoError("intent strategy version is not current")
            ledger = ExecutionLedger(options.execution_ledger)
            ledger.assert_not_attempted(audited.intent_id)
            credentials = EtoroCredentials.from_environment()
            read_client = EtoroDemoReadOnlyClient(credentials)
            control = ShadowControlStore(
                options.control
                or _default_control_path(options.shadow_log),
                event_log_path=options.shadow_log,
            ).load()
            pnl_payload = read_client.demo_pnl()
            summary = read_client.demo_summary(pnl_payload)
            shadow, _live_store = _broker_aligned_shadow(
                read_client, profile, options, control.approval_times, summary, pnl_payload
            )
            constraints = IntentConstraints(
                minimum_order_usd=options.minimum_order_usd,
                amount_increment_usd=options.amount_increment_usd,
                maximum_candle_age=timedelta(
                    minutes=options.max_candle_age_minutes
                ),
                portfolio_risk_controller=_portfolio_risk_controller(options),
            )
            current = EtoroIntentBuilder().build(
                profile,
                shadow,
                summary,
                pnl_payload,
                control,
                _live_intent_constraints(constraints, _live_store),
            )
            if not current.ready or current.intent is None:
                raise EtoroDemoError(
                    "intent is no longer ready: " + current.reason
                )
            if current.intent.intent_id != audited.intent_id:
                raise EtoroDemoError("audited intent is not the current strategy intent")
            if (
                current.intent.request_path_template != audited.request_path_template
                or dict(current.intent.request_body) != dict(audited.request_body)
            ):
                raise EtoroDemoError("audited intent payload no longer matches")
            is_close = audited.action == "close-entire-long-position"
            if not is_close:
                amount = Decimal(str(audited.request_body.get("amount")))
                if amount > options.max_demo_order_usd:
                    raise EtoroDemoError("intent exceeds --max-demo-order-usd")
            ledger.record_attempt(audited)
            execution_client = EtoroDemoExecutionClient(credentials)
            response = (
                execution_client.submit_close_long(audited)
                if is_close
                else execution_client.submit_open_long(audited)
            )
            ledger.record_response(audited.intent_id, response)
        except (EtoroDemoError, ValueError, InvalidOperation) as error:
            parser.error(str(error))
        print("eToro Demo order response received")
        print(f"Intent ID:        {audited.intent_id}")
        if is_close:
            print("Transaction:      CLOSE entire long position")
        else:
            print(f"Amount:           {audited.request_body['amount']} USD")
            print("Transaction:      BUY long")
        print("Leverage:         1x (no borrowing)")
        print("Environment:      DEMO only")
        print(f"Execution ledger: {Path(options.execution_ledger).resolve()}")
        print("Real-account access: BLOCKED")
    return 0


def _parse_timestamp(value: str, label: str = "manual approval timestamp") -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"invalid ISO-8601 {label}: {value!r}"
        ) from error
    if timestamp.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"{label} must include a UTC offset"
        )
    return timestamp


def _parse_experiment_value(profile, parameter: str, value: str) -> Decimal | int:
    if parameter in {"symbol", "strategy_version"} or not hasattr(profile, parameter):
        raise ExperimentError(f"unknown or forbidden experiment parameter: {parameter!r}")
    current = getattr(profile, parameter)
    try:
        if isinstance(current, Decimal):
            return Decimal(value)
        if isinstance(current, int) and not isinstance(current, bool):
            return int(value)
    except (InvalidOperation, ValueError) as error:
        raise ExperimentError(
            f"invalid value {value!r} for parameter {parameter!r}"
        ) from error
    raise ExperimentError(
        f"parameter {parameter!r} is not a supported numeric experiment parameter"
    )


def _format_duration(value: timedelta) -> str:
    total_seconds = int(value.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"


def _is_transient_etoro_read_error(error: Exception) -> bool:
    """Identify read-only failures that are safe to retry."""

    message = str(error).lower()
    return (
        "could not reach the etoro api" in message
        or "etoro read failed (timeouterror)" in message
        or "etoro read failed (connectionerror)" in message
        or "etoro read failed (urlerror)" in message
    )


def _default_control_path(log_path: str) -> str:
    path = Path(log_path)
    return str(path.with_suffix(".control.json"))


def _default_live_state_path(log_path: str) -> str:
    path = Path(log_path)
    return str(path.with_suffix(".live-state.json"))


def _portfolio_risk_controller(options) -> PortfolioRiskController:
    state_path = options.portfolio_risk_state or str(
        Path(options.shadow_log).parent / "portfolio-risk-state.json"
    )
    return PortfolioRiskController(
        load_portfolio_risk_policy(options.portfolio_risk_config), state_path
    )


def _live_intent_constraints(
    constraints: IntentConstraints, live_store: EtoroLiveStateStore
) -> IntentConstraints:
    state = live_store.load()
    if state is None:
        return constraints
    opened_at = None
    if state.above_entry_stable_since is not None:
        opened_at = datetime.fromisoformat(
            state.above_entry_stable_since.replace("Z", "+00:00")
        ).astimezone(UTC)
    return replace(
        constraints,
        position_opened_at=opened_at,
        existing_position_ids=state.broker_position_ids,
        liquidation_pending=state.liquidation_pending,
    )


def _latest_unresolved_intent_id(
    intent_path: Path,
    execution_path: Path,
    live_store: EtoroLiveStateStore,
) -> str | None:
    if not intent_path.exists():
        return None
    try:
        lines = [line for line in intent_path.read_text(encoding="utf-8").splitlines() if line]
        if not lines:
            return None
        value = json.loads(lines[-1])
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EtoroDemoError("intent audit is invalid while checking approval latch") from error
    if not isinstance(value, Mapping):
        raise EtoroDemoError("intent audit contains a non-object record")
    intent_id = value.get("intent_id")
    if not isinstance(intent_id, str) or value.get("execution_eligible") is not True:
        return None
    state = live_store.load()
    if state is not None and state.last_abandoned_intent_id == intent_id:
        return None
    abandonment_path = intent_path.with_name(
        intent_path.name.replace("-intents", "-abandonments")
    )
    if abandonment_path.exists():
        try:
            abandonment_lines = [
                line for line in abandonment_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            if abandonment_lines:
                abandonment = json.loads(abandonment_lines[-1])
                if (
                    isinstance(abandonment, Mapping)
                    and abandonment.get("intent_id") == intent_id
                ):
                    return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EtoroDemoError(
                "intent abandonment audit is invalid while checking approval latch"
            ) from error
    try:
        ExecutionLedger(execution_path).assert_not_attempted(intent_id)
    except EtoroDemoError:
        return None
    return intent_id


def _broker_aligned_shadow(
    client: EtoroDemoReadOnlyClient,
    profile,
    options,
    approval_times: tuple[datetime, ...],
    summary,
    pnl_payload,
):
    """Replay only candles after an immutable, flat Demo baseline."""
    store = EtoroLiveStateStore(
        options.live_state or _default_live_state_path(options.shadow_log)
    )
    state = store.load()
    runner = EtoroDryRunner(client)
    shadow = runner.run(
        profile,
        symbol=options.symbol,
        resolution=options.resolution,
        candle_count=options.candles,
        manual_approval_times=approval_times,
        trading_start_after=None if state is None else state.baseline,
    )
    if state is None:
        state = store.initialise(shadow, summary, pnl_payload)
        shadow = EtoroDryRunner(client).run(
            profile,
            symbol=options.symbol,
            resolution=options.resolution,
            candle_count=options.candles,
            manual_approval_times=approval_times,
            trading_start_after=state.baseline,
        )
    store.validate(state, shadow)
    store.validate_recorded_position(state, pnl_payload)
    store.record_scaling_observation(shadow)
    return shadow, store

