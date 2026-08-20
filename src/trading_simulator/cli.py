"""Small command-line entry point for educational project milestones."""

from __future__ import annotations

import argparse
import time
from dataclasses import replace
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .analytics import PerformanceAnalyzer
from .audit import AuditExporter, AuditExportError
from .backtest import Backtest
from .config import load_asset_profile
from .domain import Action, Decision, MarketState
from .experiments import (
    ExperimentCase,
    ExperimentError,
    OutOfSampleSplitter,
    ParameterExperiment,
)
from .etoro_demo import EtoroCredentials, EtoroDemoError, EtoroDemoReadOnlyClient
from .etoro_shadow import EtoroDryRunner, RESOLUTIONS
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
from .shadow_control import ShadowControlStore, load_latest_risk_event
from .market_data import CsvMarketDataLoader, MarketDataError
from .market_states import MarketStateClassifier
from .portfolio import Portfolio


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

    readiness_parser = subparsers.add_parser(
        "etoro-readiness-loop",
        help="continuously audit Demo readiness once per completed candle",
    )
    for action in intent_parser._actions:
        if action.dest in {"help", "intent_log"}:
            continue
        readiness_parser._add_action(action)
    readiness_parser.add_argument("--intent-log", required=True)
    readiness_parser.add_argument("--readiness-log", required=True)
    readiness_parser.add_argument("--poll-seconds", type=int, default=60)
    readiness_parser.add_argument("--max-cycles", type=int)
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
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    options = parser.parse_args(arguments)

    if options.command == "inspect-data":
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
            shadow = EtoroDryRunner(client).run(
                profile,
                symbol=options.symbol,
                resolution=options.resolution,
                candle_count=options.candles,
                manual_approval_times=control.approval_times,
            )
            pnl_payload = client.demo_pnl()
            summary = client.demo_summary(pnl_payload)
            constraints = IntentConstraints(
                minimum_order_usd=options.minimum_order_usd,
                amount_increment_usd=options.amount_increment_usd,
                maximum_candle_age=timedelta(
                    minutes=options.max_candle_age_minutes
                ),
            )
            readiness = EtoroIntentBuilder().build(
                profile,
                shadow,
                summary,
                pnl_payload,
                control,
                constraints,
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
            )
            readiness_writer = ReadinessAuditWriter(options.readiness_log)
            intent_writer = IntentAuditWriter(options.intent_log)
            shadow_writer = EtoroShadowRecorder(options.shadow_log)
        except (EtoroDemoError, ValueError) as error:
            parser.error(str(error))
        print("eToro Demo continuous execution-readiness monitor")
        print(f"Readiness log:    {readiness_writer.path.resolve()}")
        print(f"Intent log:       {intent_writer.path.resolve()}")
        print(f"Polling:          every {options.poll_seconds} seconds")
        print("Failure policy:   halt on safety/reconciliation failure")
        print("Order execution:  BLOCKED")
        print("Leverage:         1x (no borrowing)")
        print("Press Ctrl+C to stop.\n")
        cycles = 0
        try:
            while True:
                try:
                    control = control_store.load()
                    shadow = EtoroDryRunner(client).run(
                        profile,
                        symbol=options.symbol,
                        resolution=options.resolution,
                        candle_count=options.candles,
                        manual_approval_times=control.approval_times,
                    )
                    shadow_writer.record(shadow)
                    already_evaluated = readiness_writer.has_candle(
                        shadow.strategy_version,
                        shadow.requested_symbol,
                        shadow.latest_candle.timestamp,
                    )
                    if control.kill_switch:
                        print("HALTED safely: local kill switch is enabled")
                        break
                    if already_evaluated:
                        print(
                            f"{shadow.latest_candle.timestamp.isoformat()} | "
                            "already evaluated | submitted=NO"
                        )
                    else:
                        pnl_payload = client.demo_pnl()
                        summary = client.demo_summary(pnl_payload)
                        readiness = EtoroIntentBuilder().build(
                            profile,
                            shadow,
                            summary,
                            pnl_payload,
                            control,
                            constraints,
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
                except EtoroDemoError as error:
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
            shadow = EtoroDryRunner(read_client).run(
                profile,
                symbol=options.symbol,
                resolution=options.resolution,
                candle_count=options.candles,
                manual_approval_times=control.approval_times,
            )
            pnl_payload = read_client.demo_pnl()
            summary = read_client.demo_summary(pnl_payload)
            constraints = IntentConstraints(
                minimum_order_usd=options.minimum_order_usd,
                amount_increment_usd=options.amount_increment_usd,
                maximum_candle_age=timedelta(
                    minutes=options.max_candle_age_minutes
                ),
            )
            current = EtoroIntentBuilder().build(
                profile, shadow, summary, pnl_payload, control, constraints
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
            amount = Decimal(str(audited.request_body.get("amount")))
            if amount > options.max_demo_order_usd:
                raise EtoroDemoError("intent exceeds --max-demo-order-usd")
            ledger.record_attempt(audited)
            response = EtoroDemoExecutionClient(credentials).submit_open_long(audited)
            ledger.record_response(audited.intent_id, response)
        except (EtoroDemoError, ValueError, InvalidOperation) as error:
            parser.error(str(error))
        print("eToro Demo order response received")
        print(f"Intent ID:        {audited.intent_id}")
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


def _default_control_path(log_path: str) -> str:
    path = Path(log_path)
    return str(path.with_suffix(".control.json"))
