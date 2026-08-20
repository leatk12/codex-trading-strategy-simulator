"""Shared, cash-only portfolio risk gate for all Demo asset monitors."""

from __future__ import annotations

import json
import os
import time
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import Any

from .etoro_demo import EtoroDemoError, EtoroDemoPortfolioSummary


@dataclass(frozen=True, slots=True)
class PortfolioRiskPolicy:
    maximum_asset_exposure_usd: Decimal
    maximum_total_exposure_usd: Decimal
    minimum_cash_reserve_usd: Decimal
    maximum_open_assets: int
    maximum_daily_loss_rate: Decimal
    maximum_drawdown_rate: Decimal
    reservation_ttl_minutes: int = 180

    def __post_init__(self) -> None:
        money = (
            self.maximum_asset_exposure_usd,
            self.maximum_total_exposure_usd,
            self.minimum_cash_reserve_usd,
        )
        rates = (self.maximum_daily_loss_rate, self.maximum_drawdown_rate)
        if any(value <= 0 for value in money) or self.maximum_open_assets < 1:
            raise EtoroDemoError("portfolio exposure limits must be positive")
        if self.maximum_asset_exposure_usd > self.maximum_total_exposure_usd:
            raise EtoroDemoError("per-asset exposure exceeds total exposure limit")
        if any(value <= 0 or value >= 1 for value in rates):
            raise EtoroDemoError("portfolio loss rates must be between zero and one")
        if self.reservation_ttl_minutes < 1:
            raise EtoroDemoError("portfolio reservation TTL must be positive")


def load_portfolio_risk_policy(path: str | Path) -> PortfolioRiskPolicy:
    source = Path(path)
    try:
        with source.open("rb") as file:
            raw = tomllib.load(file)
        section = raw["portfolio_risk"]
        return PortfolioRiskPolicy(
            maximum_asset_exposure_usd=_decimal(
                section["maximum_asset_exposure_usd"]
            ),
            maximum_total_exposure_usd=_decimal(
                section["maximum_total_exposure_usd"]
            ),
            minimum_cash_reserve_usd=_decimal(
                section["minimum_cash_reserve_usd"]
            ),
            maximum_open_assets=int(section["maximum_open_assets"]),
            maximum_daily_loss_rate=_decimal(section["maximum_daily_loss_rate"]),
            maximum_drawdown_rate=_decimal(section["maximum_drawdown_rate"]),
            reservation_ttl_minutes=int(section.get("reservation_ttl_minutes", 180)),
        )
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        raise EtoroDemoError(f"invalid portfolio-risk config {source}") from error


@dataclass(frozen=True, slots=True)
class PortfolioRiskState:
    schema_version: int
    trading_day: str
    day_start_equity_usd: str
    peak_equity_usd: str
    latest_equity_usd: str
    available_cash_usd: str
    total_exposure_usd: str
    open_asset_count: int
    exposure_by_instrument: Mapping[str, str]
    daily_loss_rate: str
    drawdown_rate: str
    maximum_asset_exposure_usd: str
    maximum_total_exposure_usd: str
    minimum_cash_reserve_usd: str
    maximum_open_assets: int
    remaining_total_capacity_usd: str
    reserved_exposure_usd: str = "0"
    reservation_count: int = 0
    reservations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    manual_kill_switch: bool = False
    risk_halt: bool = False
    halt_reason: str | None = None
    updated_at: str = ""

    @property
    def buying_halted(self) -> bool:
        return self.manual_kill_switch or self.risk_halt


@dataclass(frozen=True, slots=True)
class PortfolioRiskAssessment:
    allowed: bool
    reason: str
    state: PortfolioRiskState


class PortfolioRiskController:
    def __init__(self, policy: PortfolioRiskPolicy, state_path: str | Path) -> None:
        self.policy = policy
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self.decision_log_path = self.state_path.with_name("portfolio-risk-decisions.jsonl")

    def assess(
        self,
        summary: EtoroDemoPortfolioSummary,
        payload: Mapping[str, Any],
        *,
        instrument_id: int,
        proposed_buy_usd: Decimal | None,
        reservation_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> PortfolioRiskAssessment:
        observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
        exposures = _portfolio_exposures(payload)
        total = sum(exposures.values(), Decimal("0"))
        with self._locked():
            previous = self._load_unlocked()
            state = self._updated_state(previous, summary, exposures, total, observed)
            removed = (
                set(previous.reservations) - set(state.reservations)
                if previous is not None else set()
            )
            if proposed_buy_usd is None:
                self._write_unlocked(state)
                self._audit_automatic_removals_unlocked(
                    previous, removed, exposures
                )
                return PortfolioRiskAssessment(True, "no portfolio buy requested", state)
            if proposed_buy_usd <= 0:
                raise EtoroDemoError("proposed portfolio buy must be positive")
            if not reservation_id or not reservation_id.strip():
                raise EtoroDemoError("portfolio buy reservation ID is required")
            existing = state.reservations.get(reservation_id)
            if existing is not None and (
                int(existing.get("instrument_id", 0)) != instrument_id
                or _decimal(existing.get("amount_usd")) != proposed_buy_usd
            ):
                raise EtoroDemoError("portfolio reservation identity conflicts")
            other_reservations = {
                key: value for key, value in state.reservations.items()
                if key != reservation_id
            }
            reserved_total = sum(
                (_decimal(value.get("amount_usd")) for value in other_reservations.values()),
                Decimal("0"),
            )
            reserved_instruments = {
                int(value.get("instrument_id", 0)) for value in other_reservations.values()
            }
            target = exposures.get(instrument_id, Decimal("0")) + proposed_buy_usd
            allowed = True
            reason = "portfolio risk checks passed and capacity reserved"
            if state.buying_halted:
                allowed = False
                reason = state.halt_reason or "portfolio buying is halted"
            elif instrument_id in reserved_instruments:
                allowed = False
                reason = "asset already has a different reserved buy intent"
            elif target > self.policy.maximum_asset_exposure_usd:
                allowed = False
                reason = "per-asset exposure limit exceeded"
            elif total + reserved_total + proposed_buy_usd > self.policy.maximum_total_exposure_usd:
                allowed = False
                reason = "total portfolio exposure limit exceeded"
            elif summary.available_cash - reserved_total - proposed_buy_usd < self.policy.minimum_cash_reserve_usd:
                allowed = False
                reason = "minimum portfolio cash reserve would be breached"
            elif (
                instrument_id not in exposures
                and instrument_id not in reserved_instruments
                and len(set(exposures) | reserved_instruments) >= self.policy.maximum_open_assets
            ):
                allowed = False
                reason = "maximum number of open assets reached"
            if allowed:
                reservations = dict(other_reservations)
                reservations[reservation_id] = {
                    "instrument_id": instrument_id,
                    "amount_usd": str(proposed_buy_usd),
                    "created_at": (
                        existing.get("created_at") if existing is not None
                        else observed.isoformat()
                    ),
                    "expires_at": (
                        existing.get("expires_at") if existing is not None
                        else (observed + timedelta(minutes=self.policy.reservation_ttl_minutes)).isoformat()
                    ),
                }
                state = self._with_reservations(state, reservations)
            self._write_unlocked(state)
            self._audit_automatic_removals_unlocked(previous, removed, exposures)
            assessment = PortfolioRiskAssessment(allowed, reason, state)
            self._append_decision_unlocked(
                assessment, instrument_id, proposed_buy_usd,
                total + reserved_total, target, observed, reservation_id
            )
        return assessment

    def release_reservation(
        self, reservation_id: str, *, changed_by: str, reason: str
    ) -> PortfolioRiskState | None:
        if not reservation_id.strip() or not changed_by.strip() or not reason.strip():
            raise EtoroDemoError("reservation ID, operator and reason are required")
        with self._locked():
            state = self._load_unlocked()
            if state is None:
                return None
            reservations = dict(state.reservations)
            if reservations.pop(reservation_id, None) is None:
                return state
            updated = self._with_reservations(state, reservations)
            self._write_unlocked(updated)
            self._append_reservation_event_unlocked(
                reservation_id, "released", changed_by, reason
            )
            return updated

    def set_manual_kill_switch(
        self, enabled: bool, *, changed_by: str, reason: str
    ) -> PortfolioRiskState:
        if not changed_by.strip() or not reason.strip():
            raise EtoroDemoError("portfolio kill-switch operator and reason are required")
        with self._locked():
            state = self._load_unlocked()
            if state is None:
                raise EtoroDemoError("portfolio risk state has not been observed yet")
            updated = PortfolioRiskState(
                **{
                    **asdict(state),
                    "manual_kill_switch": enabled,
                    "halt_reason": (
                        f"manual portfolio kill switch: {reason.strip()}"
                        if enabled
                        else (state.halt_reason if state.risk_halt else None)
                    ),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            self._write_unlocked(updated)
            return updated

    def reset_risk_halt(self, *, changed_by: str, reason: str) -> PortfolioRiskState:
        if not changed_by.strip() or not reason.strip():
            raise EtoroDemoError("portfolio reset operator and reason are required")
        with self._locked():
            state = self._load_unlocked()
            if state is None:
                raise EtoroDemoError("portfolio risk state has not been observed yet")
            equity = state.latest_equity_usd
            updated = PortfolioRiskState(
                **{
                    **asdict(state),
                    "day_start_equity_usd": equity,
                    "peak_equity_usd": equity,
                    "daily_loss_rate": "0",
                    "drawdown_rate": "0",
                    "risk_halt": False,
                    "halt_reason": (
                        state.halt_reason if state.manual_kill_switch else None
                    ),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            self._write_unlocked(updated)
            return updated

    def load(self) -> PortfolioRiskState | None:
        with self._locked():
            return self._load_unlocked()

    def _updated_state(
        self,
        previous: PortfolioRiskState | None,
        summary: EtoroDemoPortfolioSummary,
        exposures: Mapping[int, Decimal],
        total: Decimal,
        observed: datetime,
    ) -> PortfolioRiskState:
        equity = summary.equity
        if equity <= 0:
            raise EtoroDemoError("portfolio equity must be positive")
        day = observed.date().isoformat()
        new_day = previous is None or previous.trading_day != day
        baseline = equity if new_day else Decimal(previous.day_start_equity_usd)
        peak = equity if new_day else max(Decimal(previous.peak_equity_usd), equity)
        daily_loss = max(Decimal("0"), (baseline - equity) / baseline)
        drawdown = max(Decimal("0"), (peak - equity) / peak)
        prior_risk_halt = False if previous is None else previous.risk_halt
        risk_halt = prior_risk_halt
        reason = None if previous is None else previous.halt_reason
        if daily_loss >= self.policy.maximum_daily_loss_rate:
            risk_halt = True
            reason = "portfolio daily loss limit reached"
        if drawdown >= self.policy.maximum_drawdown_rate:
            risk_halt = True
            reason = "portfolio drawdown limit reached"
        manual = False if previous is None else previous.manual_kill_switch
        reservations = {} if previous is None else {
            key: value for key, value in previous.reservations.items()
            if _reservation_is_active(value, observed)
            and int(value.get("instrument_id", 0)) not in exposures
        }
        state = PortfolioRiskState(
            schema_version=1,
            trading_day=day,
            day_start_equity_usd=str(baseline),
            peak_equity_usd=str(peak),
            latest_equity_usd=str(equity),
            available_cash_usd=str(summary.available_cash),
            total_exposure_usd=str(total),
            open_asset_count=len(exposures),
            exposure_by_instrument={str(key): str(value) for key, value in exposures.items()},
            daily_loss_rate=str(daily_loss),
            drawdown_rate=str(drawdown),
            maximum_asset_exposure_usd=str(
                self.policy.maximum_asset_exposure_usd
            ),
            maximum_total_exposure_usd=str(
                self.policy.maximum_total_exposure_usd
            ),
            minimum_cash_reserve_usd=str(
                self.policy.minimum_cash_reserve_usd
            ),
            maximum_open_assets=self.policy.maximum_open_assets,
            remaining_total_capacity_usd="0",
            reservations=reservations,
            manual_kill_switch=manual,
            risk_halt=risk_halt,
            halt_reason=reason,
            updated_at=observed.isoformat(),
        )
        return self._with_reservations(state, reservations)

    def _with_reservations(
        self, state: PortfolioRiskState, reservations: Mapping[str, Mapping[str, Any]]
    ) -> PortfolioRiskState:
        reserved = sum(
            (_decimal(value.get("amount_usd")) for value in reservations.values()),
            Decimal("0"),
        )
        remaining = max(
            Decimal("0"),
            self.policy.maximum_total_exposure_usd
            - Decimal(state.total_exposure_usd)
            - reserved,
        )
        return replace(
            state,
            reserved_exposure_usd=str(reserved),
            reservation_count=len(reservations),
            reservations=dict(reservations),
            remaining_total_capacity_usd=str(remaining),
        )

    def _load_unlocked(self) -> PortfolioRiskState | None:
        if not self.state_path.exists():
            return None
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state must be an object")
            state = PortfolioRiskState(**raw)
            if state.schema_version != 1:
                raise ValueError("unsupported schema")
            return state
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise EtoroDemoError("portfolio risk state is invalid") from error

    def _write_unlocked(self, state: PortfolioRiskState) -> None:
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(asdict(state), sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        except OSError as error:
            raise EtoroDemoError("could not write portfolio risk state") from error

    def _append_decision_unlocked(
        self,
        assessment: PortfolioRiskAssessment,
        instrument_id: int,
        proposed_buy_usd: Decimal,
        current_total_usd: Decimal,
        projected_asset_usd: Decimal,
        observed: datetime,
        reservation_id: str,
    ) -> None:
        record = {
            "schema_version": 1,
            "observed_at": observed.isoformat(),
            "instrument_id": instrument_id,
            "reservation_id": reservation_id,
            "proposed_buy_usd": str(proposed_buy_usd),
            "current_total_exposure_usd": str(current_total_usd),
            "projected_asset_exposure_usd": str(projected_asset_usd),
            "projected_total_exposure_usd": str(current_total_usd + proposed_buy_usd),
            "available_cash_usd": assessment.state.available_cash_usd,
            "allowed": assessment.allowed,
            "reason": assessment.reason,
            "buying_halted": assessment.state.buying_halted,
        }
        try:
            with self.decision_log_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
        except OSError as error:
            raise EtoroDemoError("could not write portfolio risk decision audit") from error

    def _append_reservation_event_unlocked(
        self, reservation_id: str, action: str, changed_by: str, reason: str
    ) -> None:
        record = {
            "schema_version": 1,
            "observed_at": datetime.now(UTC).isoformat(),
            "reservation_id": reservation_id,
            "action": action,
            "changed_by": changed_by.strip(),
            "reason": reason.strip(),
        }
        try:
            with self.decision_log_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                file.flush()
                os.fsync(file.fileno())
        except OSError as error:
            raise EtoroDemoError("could not write portfolio reservation audit") from error

    def _audit_automatic_removals_unlocked(
        self,
        previous: PortfolioRiskState | None,
        removed: set[str],
        exposures: Mapping[int, Decimal],
    ) -> None:
        if previous is None:
            return
        for reservation_id in sorted(removed):
            reservation = previous.reservations[reservation_id]
            consumed = int(reservation.get("instrument_id", 0)) in exposures
            self._append_reservation_event_unlocked(
                reservation_id,
                "consumed" if consumed else "expired",
                "portfolio-risk-controller",
                (
                    "matching Demo position observed"
                    if consumed else "reservation TTL elapsed"
                ),
            )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 5
        descriptor = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise EtoroDemoError("portfolio risk state is locked")
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(descriptor)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass


def _portfolio_exposures(payload: Mapping[str, Any]) -> dict[int, Decimal]:
    portfolio = payload.get("clientPortfolio")
    if not isinstance(portfolio, Mapping):
        raise EtoroDemoError("eToro P&L response is missing clientPortfolio")
    positions = portfolio.get("positions", [])
    if not isinstance(positions, list) or any(not isinstance(item, Mapping) for item in positions):
        raise EtoroDemoError("Demo positions are invalid")
    exposures: dict[int, Decimal] = {}
    for position in positions:
        try:
            instrument = int(position.get("instrumentID", position.get("instrumentId")))
            leverage = int(position.get("leverage", position.get("Leverage")))
            amount = _decimal(position.get("amount", position.get("Amount")))
        except (TypeError, ValueError) as error:
            raise EtoroDemoError("Demo position risk fields are invalid") from error
        if position.get("isBuy", position.get("IsBuy")) is not True:
            raise EtoroDemoError("portfolio contains a short position")
        if leverage != 1:
            raise EtoroDemoError("portfolio contains leverage")
        if amount <= 0:
            raise EtoroDemoError("portfolio contains a non-positive position")
        if instrument in exposures:
            raise EtoroDemoError("portfolio contains duplicate same-asset positions")
        exposures[instrument] = amount
    return exposures


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise EtoroDemoError("portfolio risk number is invalid") from error
    if not result.is_finite() or result < 0:
        raise EtoroDemoError("portfolio risk number is invalid")
    return result


def _reservation_is_active(value: Mapping[str, Any], observed: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00"))
        instrument = int(value["instrument_id"])
        amount = _decimal(value["amount_usd"])
    except (KeyError, TypeError, ValueError) as error:
        raise EtoroDemoError("portfolio reservation is invalid") from error
    if expires.tzinfo is None or instrument <= 0 or amount <= 0:
        raise EtoroDemoError("portfolio reservation is invalid")
    return expires.astimezone(UTC) > observed
