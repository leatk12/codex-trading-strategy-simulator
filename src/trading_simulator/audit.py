"""Persistent, human-readable audit exports for completed backtests."""

from __future__ import annotations

import csv
import json
from dataclasses import fields
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .analytics import PerformanceReport
from .domain import StrategyResult


class AuditExportError(ValueError):
    """Raised when an audit bundle cannot be written safely."""


class AuditExporter:
    """Write a deterministic bundle without replacing existing audit files."""

    FILENAMES = (
        "manifest.json",
        "decisions.csv",
        "trades.csv",
        "equity_curve.csv",
        "performance.json",
    )

    def export(
        self,
        result: StrategyResult,
        report: PerformanceReport,
        output_directory: str | Path,
    ) -> tuple[Path, ...]:
        if result.strategy_version != report.strategy_version:
            raise AuditExportError("result and report strategy versions do not match")
        directory = Path(output_directory)
        if directory.exists() and not directory.is_dir():
            raise AuditExportError(f"output path is not a directory: {directory}")
        targets = tuple(directory / name for name in self.FILENAMES)
        existing = [path.name for path in targets if path.exists()]
        if existing:
            raise AuditExportError(
                f"refusing to overwrite existing audit files: {existing}"
            )
        directory.mkdir(parents=True, exist_ok=True)

        self._write_json(
            targets[0],
            {
                "format_version": 1,
                "strategy_version": result.strategy_version,
                "leverage_allowed": False,
                "files": list(self.FILENAMES[1:]),
            },
        )
        self._write_decisions(targets[1], result)
        self._write_trades(targets[2], result)
        self._write_equity(targets[3], report)
        self._write_json(targets[4], self._report_dict(report))
        return targets

    @staticmethod
    def _write_decisions(path: Path, result: StrategyResult) -> None:
        headers = (
            "timestamp", "action", "market_state", "price", "reason", "facts_json",
            "cash_budget", "profit_reinvestment", "reentry_stage", "strategy_version",
        )
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=headers)
            writer.writeheader()
            for decision in result.decisions:
                writer.writerow(
                    {
                        "timestamp": decision.timestamp.isoformat(),
                        "action": decision.action.value,
                        "market_state": decision.state.value,
                        "price": str(decision.price),
                        "reason": decision.reason,
                        "facts_json": json.dumps(
                            dict(sorted(decision.facts.items())),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        "cash_budget": _text(decision.cash_budget),
                        "profit_reinvestment": str(decision.profit_reinvestment),
                        "reentry_stage": _text(decision.reentry_stage),
                        "strategy_version": result.strategy_version,
                    }
                )

    @staticmethod
    def _write_trades(path: Path, result: StrategyResult) -> None:
        headers = (
            "timestamp", "symbol", "side", "quantity", "market_price",
            "simulated_price", "fees", "spread_cost", "slippage_cost",
            "total_costs", "strategy_version", "reason",
        )
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=headers)
            writer.writeheader()
            for trade in result.trades:
                writer.writerow(
                    {
                        "timestamp": trade.timestamp.isoformat(),
                        "symbol": trade.symbol,
                        "side": trade.side.value,
                        "quantity": str(trade.quantity),
                        "market_price": str(trade.market_price),
                        "simulated_price": str(trade.simulated_price),
                        "fees": str(trade.fees),
                        "spread_cost": str(trade.spread_cost),
                        "slippage_cost": str(trade.slippage_cost),
                        "total_costs": str(trade.total_costs),
                        "strategy_version": trade.strategy_version,
                        "reason": trade.reason,
                    }
                )

    @staticmethod
    def _write_equity(path: Path, report: PerformanceReport) -> None:
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(
                file, fieldnames=("timestamp", "equity", "invested", "strategy_version")
            )
            writer.writeheader()
            for point in report.equity_curve:
                writer.writerow(
                    {
                        "timestamp": point.timestamp.isoformat(),
                        "equity": str(point.value),
                        "invested": str(point.invested).lower(),
                        "strategy_version": report.strategy_version,
                    }
                )

    @staticmethod
    def _report_dict(report: PerformanceReport) -> dict[str, Any]:
        return {
            field.name: _json_value(getattr(report, field.name))
            for field in fields(report)
            if field.name != "equity_curve"
        } | {"leverage_allowed": False}

    @staticmethod
    def _write_json(path: Path, content: dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(content, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")


def _text(value: object | None) -> str:
    return "" if value is None else str(value)


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, timedelta):
        return str(value)
    return value
