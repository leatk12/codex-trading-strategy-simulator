"""Demo execution-readiness checks that only write local order intents."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import AssetProfile
from .domain import Action
from .etoro_demo import EtoroDemoError, EtoroDemoPortfolioSummary
from .etoro_shadow import EtoroDryRunResult
from .shadow_control import ShadowControlState


@dataclass(frozen=True, slots=True)
class IntentConstraints:
    minimum_order_usd: Decimal
    amount_increment_usd: Decimal
    maximum_candle_age: timedelta

    def __post_init__(self) -> None:
        if self.minimum_order_usd <= 0 or self.amount_increment_usd <= 0:
            raise EtoroDemoError("order minimum and increment must be positive")
        if self.maximum_candle_age <= timedelta(0):
            raise EtoroDemoError("maximum candle age must be positive")


@dataclass(frozen=True, slots=True)
class EtoroOrderIntent:
    intent_id: str
    created_at: datetime
    strategy_version: str
    candle_timestamp: datetime
    action: str
    request_path_template: str
    request_body: Mapping[str, Any]
    order_submitted: bool = False
    environment: str = "etoro_demo"
    leverage: int = 1


@dataclass(frozen=True, slots=True)
class IntentReadinessResult:
    ready: bool
    reason: str
    intent: EtoroOrderIntent | None = None
    halt_monitor: bool = False


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    evaluations: int
    ready: int
    rejected: int
    halting_failures: int
    first_candle: datetime | None
    last_candle: datetime | None
    reason_counts: Mapping[str, int]


class EtoroIntentBuilder:
    def __init__(self, *, now: datetime | None = None) -> None:
        self.now = (now or datetime.now(UTC)).astimezone(UTC)

    def build(
        self,
        profile: AssetProfile,
        shadow: EtoroDryRunResult,
        portfolio_summary: EtoroDemoPortfolioSummary,
        pnl_payload: Mapping[str, Any],
        control: ShadowControlState,
        constraints: IntentConstraints,
    ) -> IntentReadinessResult:
        if control.kill_switch:
            return IntentReadinessResult(False, "local kill switch is enabled", halt_monitor=True)
        if shadow.active_risk_event is not None:
            return IntentReadinessResult(False, "an unapproved risk event is active", halt_monitor=True)
        candle_completed_at = (
            shadow.latest_candle.timestamp + shadow.resolution.duration
        )
        if self.now - candle_completed_at > constraints.maximum_candle_age:
            return IntentReadinessResult(False, "latest completed candle is stale", halt_monitor=True)

        portfolio = pnl_payload.get("clientPortfolio")
        if not isinstance(portfolio, Mapping):
            raise EtoroDemoError("eToro P&L response is missing clientPortfolio")
        orders_for_open = _objects(portfolio.get("ordersForOpen", []), "ordersForOpen")
        orders = _objects(portfolio.get("orders", []), "orders")
        mirrors = _objects(portfolio.get("mirrors", []), "mirrors")
        positions = _objects(portfolio.get("positions", []), "positions")
        if orders_for_open or orders:
            return IntentReadinessResult(False, "Demo portfolio has pending orders", halt_monitor=True)
        if mirrors:
            return IntentReadinessResult(False, "Demo portfolio has copy/mirror exposure", halt_monitor=True)
        if len(positions) > 1:
            return IntentReadinessResult(False, "Demo portfolio has multiple open positions", halt_monitor=True)

        broker_position = None
        if positions:
            broker_position = positions[0]
            instrument_id = _positive_int(
                _pick(broker_position, "instrumentID", "instrumentId", "InstrumentID"),
                "positions.instrumentID",
            )
            if instrument_id != shadow.instrument_id:
                return IntentReadinessResult(False, "Demo portfolio has an unknown instrument", halt_monitor=True)
            is_buy = _pick(broker_position, "isBuy", "IsBuy")
            leverage = _pick(broker_position, "leverage", "Leverage")
            if is_buy is not True:
                return IntentReadinessResult(False, "Demo position is not a long position", halt_monitor=True)
            if _positive_int(leverage, "positions.leverage") != 1:
                return IntentReadinessResult(False, "Demo position uses leverage", halt_monitor=True)

        action = shadow.latest_decision.action
        broker_open = broker_position is not None
        expected_open_before_decision = (
            False
            if action is Action.BUY
            else True
            if action is Action.SELL
            else shadow.simulated_position_open
        )
        if broker_open != expected_open_before_decision:
            return IntentReadinessResult(
                False,
                "Demo portfolio and simulator position state do not reconcile",
                halt_monitor=True,
            )
        if action not in (Action.BUY, Action.SELL):
            return IntentReadinessResult(False, "latest strategy decision requires no order")

        if action is Action.BUY:
            amount = shadow.proposed_cash_budget
            if amount is None or amount <= 0:
                raise EtoroDemoError("BUY decision has no positive cash budget")
            if amount > portfolio_summary.available_cash:
                return IntentReadinessResult(False, "intent exceeds Demo available cash", halt_monitor=True)
            if amount > profile.maximum_position_size:
                return IntentReadinessResult(False, "intent exceeds maximum position size", halt_monitor=True)
            if amount < constraints.minimum_order_usd:
                return IntentReadinessResult(False, "intent is below configured order minimum", halt_monitor=True)
            if amount % constraints.amount_increment_usd != 0:
                return IntentReadinessResult(False, "intent violates amount increment", halt_monitor=True)
            path = "/api/v2/trading/execution/demo/orders"
            body: Mapping[str, Any] = {
                "action": "open",
                "transaction": "buy",
                "instrumentId": shadow.instrument_id,
                "orderType": "mkt",
                "amount": str(amount),
                "orderCurrency": "usd",
                "leverage": 1,
            }
            intent_action = "open-long-by-cash-amount"
        else:
            if broker_position is None:
                return IntentReadinessResult(False, "no reconciled Demo position to close", halt_monitor=True)
            position_id = _positive_int(
                _pick(broker_position, "positionID", "positionId", "PositionID"),
                "positions.positionID",
            )
            path = (
                "/api/v1/trading/execution/demo/market-close-orders/positions/"
                f"{position_id}"
            )
            body = {"UnitsToDeduct": None}
            intent_action = "close-entire-long-position"

        identity = "|".join(
            (
                profile.strategy_version,
                shadow.requested_symbol,
                shadow.latest_candle.timestamp.isoformat(),
                intent_action,
                json.dumps(body, sort_keys=True),
            )
        )
        intent = EtoroOrderIntent(
            intent_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            created_at=self.now,
            strategy_version=profile.strategy_version,
            candle_timestamp=shadow.latest_candle.timestamp,
            action=intent_action,
            request_path_template=path,
            request_body=body,
        )
        return IntentReadinessResult(True, "all non-executing readiness checks passed", intent)


class IntentAuditWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, result: IntentReadinessResult) -> bool:
        intent = result.intent
        if intent is None:
            return False
        if self._contains(intent.intent_id):
            return False
        record = {
            "schema_version": 1,
            "intent_id": intent.intent_id,
            "created_at": intent.created_at.isoformat(),
            "environment": intent.environment,
            "strategy_version": intent.strategy_version,
            "candle_timestamp": intent.candle_timestamp.isoformat(),
            "action": intent.action,
            "request_path_template": intent.request_path_template,
            "request_body": dict(intent.request_body),
            "leverage": 1,
            "borrowing_allowed": False,
            "order_submitted": False,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                file.write("\n")
                file.flush()
        except OSError as error:
            raise EtoroDemoError(f"could not append intent audit: {error}") from error
        return True

    def _contains(self, intent_id: str) -> bool:
        if not self.path.exists():
            return False
        try:
            with self.path.open("r", encoding="utf-8") as file:
                for line in file:
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if not isinstance(value, Mapping):
                        raise ValueError("intent record must be an object")
                    if value.get("intent_id") == intent_id:
                        return True
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise EtoroDemoError(f"invalid existing intent audit {self.path}") from error
        return False


class ReadinessAuditWriter:
    """Record one accepted or rejected readiness outcome per candle."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def has_candle(self, strategy_version: str, symbol: str, candle: datetime) -> bool:
        identity = _evaluation_id(strategy_version, symbol, candle)
        return any(record.get("evaluation_id") == identity for record in self._records())

    def append(
        self,
        shadow: EtoroDryRunResult,
        result: IntentReadinessResult,
        summary: EtoroDemoPortfolioSummary,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        identity = _evaluation_id(
            shadow.strategy_version,
            shadow.requested_symbol,
            shadow.latest_candle.timestamp,
        )
        if any(record.get("evaluation_id") == identity for record in self._records()):
            return False
        observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
        record = {
            "schema_version": 1,
            "evaluation_id": identity,
            "observed_at": observed.isoformat(),
            "environment": "etoro_demo",
            "mode": "read_only_readiness",
            "strategy_version": shadow.strategy_version,
            "symbol": shadow.requested_symbol,
            "candle_timestamp": shadow.latest_candle.timestamp.isoformat(),
            "decision": shadow.latest_decision.action.value,
            "market_state": shadow.latest_decision.state.value,
            "ready": result.ready,
            "halt_monitor": result.halt_monitor,
            "reason": result.reason,
            "intent_id": None if result.intent is None else result.intent.intent_id,
            "available_cash": str(summary.available_cash),
            "open_positions": summary.open_position_count,
            "pending_orders": summary.pending_order_count,
            "leverage": 1,
            "borrowing_allowed": False,
            "order_submitted": False,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                file.write("\n")
                file.flush()
        except OSError as error:
            raise EtoroDemoError(f"could not append readiness audit: {error}") from error
        return True

    def _records(self) -> list[Mapping[str, Any]]:
        if not self.path.exists():
            return []
        records: list[Mapping[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as file:
                for line in file:
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if not isinstance(value, Mapping):
                        raise ValueError("readiness record must be an object")
                    records.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise EtoroDemoError(f"invalid readiness audit {self.path}") from error
        return records

    def report(self) -> ReadinessReport:
        records = self._records()
        timestamps: list[datetime] = []
        reasons: dict[str, int] = {}
        ready = 0
        halting = 0
        for record in records:
            try:
                timestamp = datetime.fromisoformat(
                    str(record["candle_timestamp"]).replace("Z", "+00:00")
                )
            except (KeyError, ValueError) as error:
                raise EtoroDemoError(
                    f"invalid readiness audit timestamp in {self.path}"
                ) from error
            if timestamp.tzinfo is None:
                raise EtoroDemoError(
                    f"readiness audit timestamp has no timezone in {self.path}"
                )
            timestamps.append(timestamp.astimezone(UTC))
            if record.get("ready") is True:
                ready += 1
            if record.get("halt_monitor") is True:
                halting += 1
            reason = str(record.get("reason", "missing reason"))
            reasons[reason] = reasons.get(reason, 0) + 1
        return ReadinessReport(
            evaluations=len(records),
            ready=ready,
            rejected=len(records) - ready,
            halting_failures=halting,
            first_candle=min(timestamps) if timestamps else None,
            last_candle=max(timestamps) if timestamps else None,
            reason_counts=dict(sorted(reasons.items())),
        )


def _evaluation_id(strategy_version: str, symbol: str, candle: datetime) -> str:
    identity = "|".join((strategy_version, symbol, candle.isoformat()))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _objects(value: object, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise EtoroDemoError(f"eToro field {name} must be a list of objects")
    return value


def _pick(value: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return None


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise EtoroDemoError(f"eToro field {name} must be a positive integer")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise EtoroDemoError(f"eToro field {name} must be a positive integer") from error
    if result <= 0:
        raise EtoroDemoError(f"eToro field {name} must be a positive integer")
    return result
