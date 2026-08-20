"""Persistent broker-aligned baseline for eToro Demo readiness monitoring."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from .etoro_demo import EtoroDemoError, EtoroDemoPortfolioSummary
from .etoro_shadow import EtoroDryRunResult


@dataclass(frozen=True, slots=True)
class EtoroLiveState:
    schema_version: int
    strategy_version: str
    requested_symbol: str
    profile_symbol: str
    instrument_id: int
    resolution: str
    baseline_candle_timestamp: str
    baseline_broker_position_open: bool
    created_at: str
    broker_position_id: int | None = None
    broker_amount_usd: str | None = None
    broker_units: str | None = None
    broker_open_rate: str | None = None
    reconciled_intent_id: str | None = None
    reconciled_at: str | None = None
    broker_fees_usd: str | None = None
    generation: int = 0
    last_abandoned_intent_id: str | None = None
    last_rebaseline_at: str | None = None
    broker_position_ids: tuple[int, ...] = ()
    scale_in_count: int = 0
    liquidation_pending: bool = False
    above_entry_stable_since: str | None = None

    @property
    def baseline(self) -> datetime:
        value = datetime.fromisoformat(
            self.baseline_candle_timestamp.replace("Z", "+00:00")
        )
        if value.tzinfo is None:
            raise EtoroDemoError("live-state baseline timestamp has no timezone")
        return value.astimezone(UTC)


class EtoroLiveStateStore:
    """Create once, then validate the immutable identity of a live baseline."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> EtoroLiveState | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("state must be an object")
            if "broker_position_ids" in value:
                raw_ids = value["broker_position_ids"]
                if not isinstance(raw_ids, list):
                    raise ValueError("broker_position_ids must be a list")
                value["broker_position_ids"] = tuple(int(item) for item in raw_ids)
            state = EtoroLiveState(**value)
            if state.schema_version != 1:
                raise ValueError("unsupported schema version")
            state.baseline
            if not state.broker_position_ids and state.broker_position_id is not None:
                state = replace(state, broker_position_ids=(state.broker_position_id,))
            return state
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise EtoroDemoError(f"invalid live-state checkpoint {self.path}") from error

    def initialise(
        self,
        shadow: EtoroDryRunResult,
        summary: EtoroDemoPortfolioSummary,
        pnl_payload: Mapping[str, Any] | None = None,
        *,
        observed_at: datetime | None = None,
    ) -> EtoroLiveState:
        existing = self.load()
        if existing is not None:
            self.validate(existing, shadow)
            return existing
        target_positions = _instrument_positions(pnl_payload, shadow.instrument_id)
        if target_positions:
            raise EtoroDemoError(
                "cannot establish a new live baseline while this instrument "
                "has an open position; close or explicitly reconcile it first"
            )
        if summary.pending_order_count != 0:
            raise EtoroDemoError(
                "cannot establish a new live baseline while Demo orders are pending"
            )
        created = (observed_at or datetime.now(UTC)).astimezone(UTC)
        state = EtoroLiveState(
            schema_version=1,
            strategy_version=shadow.strategy_version,
            requested_symbol=shadow.requested_symbol,
            profile_symbol=shadow.profile_symbol,
            instrument_id=shadow.instrument_id,
            resolution=shadow.resolution.cli_name,
            baseline_candle_timestamp=shadow.latest_candle.timestamp.isoformat(),
            baseline_broker_position_open=False,
            created_at=created.isoformat(),
        )
        self._write(state)
        return state

    def validate(self, state: EtoroLiveState, shadow: EtoroDryRunResult) -> None:
        expected = (
            state.strategy_version,
            state.requested_symbol,
            state.profile_symbol,
            state.instrument_id,
            state.resolution,
        )
        actual = (
            shadow.strategy_version,
            shadow.requested_symbol,
            shadow.profile_symbol,
            shadow.instrument_id,
            shadow.resolution.cli_name,
        )
        if actual != expected:
            raise EtoroDemoError(
                "live-state checkpoint belongs to a different strategy or instrument"
            )

    def record_open_position(
        self,
        *,
        intent_id: str,
        position_id: int,
        amount_usd: str,
        units: str,
        open_rate: str,
        fees_usd: str | None = None,
        observed_at: datetime | None = None,
    ) -> EtoroLiveState:
        state = self.load()
        if state is None:
            raise EtoroDemoError("live-state checkpoint does not exist")
        ids = state.broker_position_ids
        is_new_lot = position_id not in ids
        ids = ids if not is_new_lot else (*ids, position_id)
        prior_amount = Decimal(state.broker_amount_usd or "0")
        prior_units = Decimal(state.broker_units or "0")
        lot_amount = Decimal(amount_usd)
        lot_units = Decimal(units)
        total_amount = prior_amount + (lot_amount if is_new_lot else Decimal("0"))
        total_units = prior_units + (lot_units if is_new_lot else Decimal("0"))
        if state.broker_position_id is None:
            total_amount, total_units = lot_amount, lot_units
        updated = replace(
            state,
            broker_position_id=ids[0],
            broker_position_ids=ids,
            broker_amount_usd=str(total_amount),
            broker_units=str(total_units),
            broker_open_rate=str(total_amount / total_units),
            broker_fees_usd=fees_usd,
            reconciled_intent_id=intent_id,
            reconciled_at=(observed_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
            scale_in_count=max(len(ids) - 1, 0),
            liquidation_pending=False,
            above_entry_stable_since=None,
        )
        self._write(updated)
        return updated

    def validate_recorded_position(
        self, state: EtoroLiveState, payload: Mapping[str, Any]
    ) -> None:
        if state.broker_position_id is None:
            return
        portfolio = payload.get("clientPortfolio")
        positions = None if not isinstance(portfolio, Mapping) else portfolio.get("positions")
        if not isinstance(positions, list) or any(not isinstance(item, Mapping) for item in positions):
            raise EtoroDemoError("recorded Demo position is missing or ambiguous")
        ids = state.broker_position_ids or (state.broker_position_id,)
        same_instrument_ids = {
            _safe_int(item.get("positionID", item.get("positionId")))
            for item in positions
            if _safe_int(item.get("instrumentID", item.get("instrumentId")))
            == state.instrument_id
        }
        if same_instrument_ids != set(ids):
            raise EtoroDemoError(
                "Demo positions for this instrument do not match recorded live lots"
            )
        matches = [
            item for item in positions
            if _safe_int(item.get("positionID", item.get("positionId")))
            in ids
        ]
        if len(matches) != len(ids):
            raise EtoroDemoError("recorded Demo position is missing or ambiguous")
        for position in matches:
            try:
                instrument_id = int(position.get("instrumentID", position.get("instrumentId")))
                leverage = int(position.get("leverage", position.get("Leverage")))
            except (TypeError, ValueError) as error:
                raise EtoroDemoError("recorded Demo position fields are invalid") from error
            if instrument_id != state.instrument_id:
                raise EtoroDemoError("Demo position no longer matches the recorded live state")
            if position.get("isBuy", position.get("IsBuy")) is not True or leverage != 1:
                raise EtoroDemoError("recorded Demo position violates long-only 1x state")

    def rebaseline_flat(
        self,
        *,
        baseline: datetime,
        abandoned_intent_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> EtoroLiveState:
        state = self.load()
        if state is None:
            raise EtoroDemoError("live-state checkpoint does not exist")
        if baseline.tzinfo is None:
            raise EtoroDemoError("replacement baseline must include a timezone")
        if state.broker_position_id is not None:
            raise EtoroDemoError("cannot rebaseline a checkpoint with a recorded position")
        if baseline < state.baseline:
            raise EtoroDemoError("replacement baseline cannot move backwards")
        updated = replace(
            state,
            baseline_candle_timestamp=baseline.astimezone(UTC).isoformat(),
            baseline_broker_position_open=False,
            generation=state.generation + 1,
            last_abandoned_intent_id=abandoned_intent_id,
            last_rebaseline_at=(observed_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
            above_entry_stable_since=None,
        )
        self._write(updated)
        return updated

    def record_closed_position(
        self,
        *,
        intent_id: str,
        position_id: int,
        pnl_payload: Mapping[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> EtoroLiveState:
        state = self.load()
        if state is None:
            raise EtoroDemoError("live state is not bound to the closed position")
        ids = state.broker_position_ids or (
            (() if state.broker_position_id is None else (state.broker_position_id,))
        )
        if position_id not in ids:
            raise EtoroDemoError("live state is not bound to the closed position")
        remaining = tuple(item for item in ids if item != position_id)
        remaining_amount = state.broker_amount_usd if remaining else None
        remaining_units = state.broker_units if remaining else None
        remaining_rate = state.broker_open_rate if remaining else None
        if remaining and pnl_payload is not None:
            lots = _instrument_positions(pnl_payload, state.instrument_id)
            lots = [
                item for item in lots
                if _safe_int(item.get("positionID", item.get("positionId")))
                in remaining
            ]
            if len(lots) != len(remaining):
                raise EtoroDemoError("remaining Demo lots do not match live state")
            amount = sum(
                (Decimal(str(item.get("amount", item.get("Amount", "0")))) for item in lots),
                Decimal("0"),
            )
            units = sum(
                (
                    Decimal(str(item.get("units", item.get("Units"))))
                    if item.get("units", item.get("Units")) is not None
                    else Decimal(str(item.get("amount", item.get("Amount", "0"))))
                    / Decimal(str(item.get("openRate", item.get("OpenRate", "0"))))
                    for item in lots
                ),
                Decimal("0"),
            )
            remaining_amount = str(amount)
            remaining_units = str(units)
            remaining_rate = str(amount / units)
        updated = replace(
            state,
            broker_position_id=remaining[0] if remaining else None,
            broker_position_ids=remaining,
            broker_amount_usd=remaining_amount,
            broker_units=remaining_units,
            broker_open_rate=remaining_rate,
            broker_fees_usd=state.broker_fees_usd if remaining else None,
            scale_in_count=max(len(remaining) - 1, 0),
            liquidation_pending=bool(remaining),
            above_entry_stable_since=None,
            reconciled_intent_id=intent_id,
            reconciled_at=(observed_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
        )
        self._write(updated)
        return updated

    def record_scaling_observation(
        self, shadow: EtoroDryRunResult
    ) -> EtoroLiveState:
        state = self.load()
        if state is None:
            raise EtoroDemoError("live-state checkpoint does not exist")
        qualifies = (
            state.broker_position_id is not None
            and state.broker_open_rate is not None
            and shadow.latest_candle.close > Decimal(state.broker_open_rate)
            and shadow.latest_decision.state.value in {"stabilising", "recovering"}
        )
        since = state.above_entry_stable_since
        replacement = (
            (since or shadow.latest_candle.timestamp.isoformat())
            if qualifies
            else None
        )
        if replacement == since:
            return state
        updated = replace(state, above_entry_stable_since=replacement)
        self._write(updated)
        return updated

    def record_abandoned_intent(self, intent_id: str) -> EtoroLiveState:
        state = self.load()
        if state is None:
            raise EtoroDemoError("live-state checkpoint does not exist")
        updated = replace(state, last_abandoned_intent_id=intent_id)
        self._write(updated)
        return updated

    def _write(self, state: EtoroLiveState) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(asdict(state), sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as error:
            raise EtoroDemoError(f"could not write live-state checkpoint: {error}") from error


def _safe_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _instrument_positions(
    payload: Mapping[str, Any] | None, instrument_id: int
) -> list[Mapping[str, Any]]:
    if payload is None:
        return []
    portfolio = payload.get("clientPortfolio")
    positions = None if not isinstance(portfolio, Mapping) else portfolio.get("positions")
    if not isinstance(positions, list) or any(not isinstance(item, Mapping) for item in positions):
        raise EtoroDemoError("Demo positions are missing or invalid")
    return [
        item for item in positions
        if _safe_int(item.get("instrumentID", item.get("instrumentId"))) == instrument_id
    ]
