"""Read-only reconciliation of one submitted eToro Demo long-buy intent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .etoro_demo import EtoroDemoError
from .etoro_demo_execution import AuditedIntent


MAX_EXECUTION_ROUNDING_SHORTFALL_USD = Decimal("0.01")
MAX_EXECUTION_ROUNDING_SHORTFALL_RATE = Decimal("0.0001")
ABSOLUTE_EXECUTION_ROUNDING_CAP_USD = Decimal("0.10")


@dataclass(frozen=True, slots=True)
class ReconciledDemoPosition:
    position_id: int
    instrument_id: int
    amount_usd: Decimal
    units: Decimal
    open_rate: Decimal
    fees_usd: Decimal | None = None

    def audit_fields(self) -> Mapping[str, Any]:
        return {
            "position_id": self.position_id,
            "instrument_id": self.instrument_id,
            "amount_usd": str(self.amount_usd),
            "units": str(self.units),
            "open_rate": str(self.open_rate),
            "fees_usd": None if self.fees_usd is None else str(self.fees_usd),
            "fees_source": (
                "not_exposed_by_demo_pnl"
                if self.fees_usd is None
                else "demo_pnl_position"
            ),
        }


@dataclass(frozen=True, slots=True)
class ReconciledDemoClose:
    position_id: int
    instrument_id: int

    def audit_fields(self) -> Mapping[str, Any]:
        return {
            "position_id": self.position_id,
            "instrument_id": self.instrument_id,
            "position_closed": True,
        }


def reconcile_open_long(
    intent: AuditedIntent,
    payload: Mapping[str, Any],
    *,
    existing_position_ids: tuple[int, ...] = (),
) -> ReconciledDemoPosition | None:
    """Return None while pending; raise on unsafe or ambiguous broker state."""
    portfolio = payload.get("clientPortfolio")
    if not isinstance(portfolio, Mapping):
        raise EtoroDemoError("eToro P&L response is missing clientPortfolio")
    mirrors = _objects(portfolio.get("mirrors", []), "mirrors")
    positions = _objects(portfolio.get("positions", []), "positions")
    pending = _objects(portfolio.get("orders", []), "orders") + _objects(
        portfolio.get("ordersForOpen", []), "ordersForOpen"
    )
    if mirrors:
        raise EtoroDemoError("Demo portfolio has copy/mirror exposure")
    expected_instrument = _positive_int(intent.request_body.get("instrumentId"), "intent instrument")
    _validate_portfolio_positions(positions)
    matches = [
        position for position in positions
        if _positive_int(
            _pick(position, "instrumentID", "instrumentId", "InstrumentID"),
            "position instrument",
        ) == expected_instrument
    ]
    if not matches:
        return None
    if pending:
        raise EtoroDemoError("Demo position appeared while an order remains pending")
    new_matches = [
        position for position in matches
        if _positive_int(
            _pick(position, "positionID", "positionId", "PositionID"),
            "position ID",
        ) not in existing_position_ids
    ]
    if not new_matches:
        return None
    if len(new_matches) != 1:
        raise EtoroDemoError("expected exactly one new Demo position for the submitted instrument")
    position = new_matches[0]
    instrument = _positive_int(_pick(position, "instrumentID", "instrumentId", "InstrumentID"), "position instrument")
    if instrument != expected_instrument:
        raise EtoroDemoError("Demo position instrument does not match submitted intent")
    if _pick(position, "isBuy", "IsBuy") is not True:
        raise EtoroDemoError("reconciled Demo position is not long")
    if _positive_int(_pick(position, "leverage", "Leverage"), "position leverage") != 1:
        raise EtoroDemoError("reconciled Demo position uses leverage")
    amount = _positive_decimal(_pick(position, "amount", "Amount"), "position amount")
    intended = _positive_decimal(intent.request_body.get("amount"), "intent amount")
    raw_fees = _pick(
        position, "totalExternalCosts", "TotalExternalCosts", "fees", "Fees"
    )
    fees = None if raw_fees is None else _nonnegative_decimal(raw_fees, "position fees")
    shortfall = intended - amount
    accounted_amount = amount + (fees or Decimal("0"))
    accounted_difference = intended - accounted_amount
    permitted_difference = min(
        max(
            MAX_EXECUTION_ROUNDING_SHORTFALL_USD,
            intended * MAX_EXECUTION_ROUNDING_SHORTFALL_RATE,
        ),
        ABSOLUTE_EXECUTION_ROUNDING_CAP_USD,
    )
    if (
        shortfall < 0
        or accounted_difference < 0
        or accounted_difference > permitted_difference
    ):
        raise EtoroDemoError(
            "Demo position amount indicates a partial or altered fill "
            f"(intended={intended}, observed={amount}, "
            f"external_costs={fees or Decimal('0')}, "
            f"unaccounted_difference={accounted_difference}, "
            f"permitted_rounding={permitted_difference})"
        )
    open_rate = _positive_decimal(
        _pick(position, "openRate", "OpenRate", "openPrice", "OpenPrice"),
        "position open rate",
    )
    raw_units = _pick(position, "units", "Units", "unitAmount", "UnitAmount")
    units = amount / open_rate if raw_units is None else _positive_decimal(raw_units, "position units")
    return ReconciledDemoPosition(
        position_id=_positive_int(_pick(position, "positionID", "positionId", "PositionID"), "position ID"),
        instrument_id=instrument,
        amount_usd=amount,
        units=units,
        open_rate=open_rate,
        fees_usd=fees,
    )


def reconcile_closed_long(
    intent: AuditedIntent,
    payload: Mapping[str, Any],
    *,
    expected_instrument_id: int,
    allowed_remaining_position_ids: tuple[int, ...] = (),
) -> ReconciledDemoClose | None:
    """Confirm that the exact fully closed position is absent; allow other assets."""

    portfolio = payload.get("clientPortfolio")
    if not isinstance(portfolio, Mapping):
        raise EtoroDemoError("eToro P&L response is missing clientPortfolio")
    mirrors = _objects(portfolio.get("mirrors", []), "mirrors")
    positions = _objects(portfolio.get("positions", []), "positions")
    pending = _objects(portfolio.get("orders", []), "orders") + _objects(
        portfolio.get("ordersForOpen", []), "ordersForOpen"
    )
    if mirrors:
        raise EtoroDemoError("Demo portfolio has copy/mirror exposure")
    if pending:
        return None
    _validate_portfolio_positions(positions)
    if intent.action != "close-entire-long-position":
        raise EtoroDemoError("intent is not a full-position close")
    raw_position_id = intent.request_path_template.rsplit("/", 1)[-1]
    position_id = _positive_int(raw_position_id, "close intent position ID")
    if any(
        _positive_int(
            _pick(position, "positionID", "positionId", "PositionID"),
            "position ID",
        ) == position_id
        for position in positions
    ):
        return None
    remaining_for_instrument = [
        position for position in positions
        if _positive_int(
            _pick(position, "instrumentID", "instrumentId", "InstrumentID"),
            "position instrument",
        ) == expected_instrument_id
    ]
    remaining_ids = {
        _positive_int(
            _pick(position, "positionID", "positionId", "PositionID"),
            "position ID",
        )
        for position in remaining_for_instrument
    }
    if remaining_ids != set(allowed_remaining_position_ids):
        raise EtoroDemoError(
            "remaining positions for the closed instrument are ambiguous or do not match live state"
        )
    return ReconciledDemoClose(
        position_id=position_id, instrument_id=expected_instrument_id
    )


def _validate_portfolio_positions(positions: list[Mapping[str, Any]]) -> None:
    for position in positions:
        if _pick(position, "isBuy", "IsBuy") is not True:
            raise EtoroDemoError("Demo portfolio contains a short position")
        if _positive_int(
            _pick(position, "leverage", "Leverage"), "position leverage"
        ) != 1:
            raise EtoroDemoError("Demo portfolio contains leverage")


def _objects(value: object, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise EtoroDemoError(f"eToro field {name} must be a list of objects")
    return list(value)


def _pick(value: Mapping[str, Any], *names: str) -> Any:
    return next((value[name] for name in names if name in value), None)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise EtoroDemoError(f"{name} must be a positive integer")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise EtoroDemoError(f"{name} must be a positive integer") from error
    if result <= 0:
        raise EtoroDemoError(f"{name} must be a positive integer")
    return result


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise EtoroDemoError(f"{name} must be a positive number") from error
    if not result.is_finite() or result <= 0:
        raise EtoroDemoError(f"{name} must be a positive number")
    return result


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise EtoroDemoError(f"{name} must be a non-negative number") from error
    if not result.is_finite() or result < 0:
        raise EtoroDemoError(f"{name} must be a non-negative number")
    return result
