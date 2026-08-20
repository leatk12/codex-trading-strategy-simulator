"""One-shot eToro market-data replay that can never submit an order."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
import hashlib

from .backtest import Backtest, BacktestError
from .config import AssetProfile
from .domain import Action, Decision, MarketSnapshot
from .etoro_demo import EtoroDemoError
from .market_data import HistoricalMarketData, MarketDataError


class EtoroMarketDataClient(Protocol):
    def market_data(
        self, resource: str, parameters: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class EtoroResolution:
    cli_name: str
    api_name: str
    duration: timedelta


RESOLUTIONS = {
    item.cli_name: item
    for item in (
        EtoroResolution("one-minute", "OneMinute", timedelta(minutes=1)),
        EtoroResolution("five-minutes", "FiveMinutes", timedelta(minutes=5)),
        EtoroResolution("ten-minutes", "TenMinutes", timedelta(minutes=10)),
        EtoroResolution("fifteen-minutes", "FifteenMinutes", timedelta(minutes=15)),
        EtoroResolution("thirty-minutes", "ThirtyMinutes", timedelta(minutes=30)),
        EtoroResolution("one-hour", "OneHour", timedelta(hours=1)),
        EtoroResolution("four-hours", "FourHours", timedelta(hours=4)),
        EtoroResolution("one-day", "OneDay", timedelta(days=1)),
        EtoroResolution("one-week", "OneWeek", timedelta(weeks=1)),
    )
}


@dataclass(frozen=True, slots=True)
class EtoroDryRunResult:
    strategy_version: str
    requested_symbol: str
    profile_symbol: str
    resolution: EtoroResolution
    completed_candle_count: int
    latest_candle: MarketSnapshot
    latest_decision: Decision
    proposed_action: str
    proposed_cash_budget: Decimal | None
    active_risk_event: "ShadowRiskEvent | None" = None
    instrument_id: int = 0
    simulated_position_open: bool = False
    leverage: int = 1
    order_submitted: bool = False


@dataclass(frozen=True, slots=True)
class ShadowRiskEvent:
    event_id: str
    triggered_at: datetime
    reasons: tuple[str, ...]
    evidence: Mapping[str, str]


class EtoroDryRunner:
    """Resolve an instrument, replay completed candles, and report one proposal."""

    def __init__(
        self,
        client: EtoroMarketDataClient,
        *,
        now: datetime | None = None,
    ) -> None:
        self.client = client
        self.now = now or datetime.now(UTC)
        if self.now.tzinfo is None:
            raise EtoroDemoError("dry-run clock must include a timezone")
        self.now = self.now.astimezone(UTC)

    def run(
        self,
        profile: AssetProfile,
        *,
        symbol: str,
        resolution: str,
        candle_count: int,
        manual_approval_at: datetime | None = None,
        manual_approval_times: tuple[datetime, ...] = (),
        trading_start_after: datetime | None = None,
    ) -> EtoroDryRunResult:
        clean_symbol = symbol.strip()
        if not clean_symbol:
            raise EtoroDemoError("eToro symbol must not be empty")
        if resolution not in RESOLUTIONS:
            raise EtoroDemoError(f"unsupported eToro resolution: {resolution!r}")
        if not 2 <= candle_count <= 1000:
            raise EtoroDemoError("--candles must be between 2 and 1000")

        instrument_id = self._resolve_instrument(clean_symbol)
        interval = RESOLUTIONS[resolution]
        payload = self.client.market_data(
            f"instruments/{instrument_id}/history/candles/desc/"
            f"{interval.api_name}/{candle_count}"
        )
        candles = self._extract_candles(payload, profile.symbol)
        completed = sorted(
            (
                candle
                for candle in candles
                if candle.timestamp + interval.duration <= self.now
            ),
            key=lambda candle: candle.timestamp,
        )
        # Some API responses may overlap or repeat a candle. Reject rather than
        # allowing ambiguous market history into the strategy.
        completed = completed[-candle_count:]
        if len(completed) < 2:
            raise EtoroDemoError(
                "eToro returned fewer than two completed candles; try again later"
            )
        try:
            data = HistoricalMarketData(completed)
            if (
                trading_start_after is not None
                and trading_start_after < data[0].timestamp
            ):
                raise EtoroDemoError(
                    "live-state baseline is older than the downloaded candle "
                    "window; increase --candles before monitoring"
                )
            result = Backtest(
                profile,
                data,
                manual_approval_at=manual_approval_at,
                manual_approval_times=manual_approval_times,
                trading_start_after=trading_start_after,
            ).run()
        except EtoroDemoError:
            raise
        except (MarketDataError, BacktestError, ValueError) as error:
            raise EtoroDemoError(f"could not run the local replay: {error}") from error

        decision = result.decisions[-1]
        active_risk_event = _active_risk_event(
            result.decisions, profile.strategy_version, profile.symbol
        )
        proposed_action = {
            Action.BUY: "buy",
            Action.SELL: "close-long",
            Action.HOLD: "none",
            Action.SUSPEND_AUTOMATIC_BUYING: "none",
        }[decision.action]
        return EtoroDryRunResult(
            strategy_version=result.strategy_version,
            requested_symbol=clean_symbol.upper(),
            profile_symbol=profile.symbol,
            resolution=interval,
            completed_candle_count=len(data),
            latest_candle=data[-1],
            latest_decision=decision,
            proposed_action=proposed_action,
            proposed_cash_budget=decision.cash_budget,
            active_risk_event=active_risk_event,
            instrument_id=instrument_id,
            simulated_position_open=(
                bool(result.trades)
                and result.trades[-1].side.value == "buy"
            ),
        )

    def _resolve_instrument(self, symbol: str) -> int:
        payload = self.client.market_data(
            "search",
            {
                "internalSymbolFull": symbol,
                "fields": "instrumentId,internalSymbolFull,displayname",
            },
        )
        candidates = _find_object_list(
            payload, ("items", "instruments", "data", "searchResults")
        )
        exact = [
            item
            for item in candidates
            if str(_pick(item, "internalSymbolFull", "InternalSymbolFull") or "")
            .strip()
            .casefold()
            == symbol.strip().casefold()
        ]
        if len(exact) != 1:
            raise EtoroDemoError(
                f"expected one exact eToro instrument match for {symbol!r}; "
                f"received {len(exact)}"
            )
        raw_id = _pick(exact[0], "instrumentId", "instrumentID", "InstrumentID")
        if isinstance(raw_id, bool):
            raw_id = None
        try:
            instrument_id = int(raw_id)
        except (TypeError, ValueError) as error:
            raise EtoroDemoError("exact eToro match has no valid instrumentId") from error
        if instrument_id <= 0:
            raise EtoroDemoError("exact eToro match has no valid instrumentId")
        return instrument_id

    @staticmethod
    def _extract_candles(
        payload: Mapping[str, Any], profile_symbol: str
    ) -> list[MarketSnapshot]:
        rows = _find_object_list(payload, ("candles", "Candles", "data"))
        # eToro wraps each instrument's actual OHLCV rows inside an outer
        # `candles` item. A few older examples expose the rows directly, so
        # accept both documented shapes without inspecting or logging values.
        nested_rows: list[Mapping[str, Any]] = []
        for row in rows:
            nested = _pick(row, "candles", "Candles")
            if isinstance(nested, list):
                if any(not isinstance(item, Mapping) for item in nested):
                    raise EtoroDemoError(
                        "eToro nested candles field must contain objects"
                    )
                nested_rows.extend(nested)
        if nested_rows:
            rows = nested_rows
        snapshots: list[MarketSnapshot] = []
        for number, row in enumerate(rows, start=1):
            try:
                snapshots.append(
                    MarketSnapshot(
                        symbol=profile_symbol,
                        timestamp=_timestamp(
                            _pick(
                                row,
                                "fromDate",
                                "FromDate",
                                "from",
                                "From",
                                "timestamp",
                                "Timestamp",
                            ),
                            number,
                        ),
                        open=_number(_pick(row, "open", "Open"), "open", number),
                        high=_number(_pick(row, "high", "High"), "high", number),
                        low=_number(_pick(row, "low", "Low"), "low", number),
                        close=_number(_pick(row, "close", "Close"), "close", number),
                        volume=_number(
                            _pick(row, "volume", "Volume") or 0, "volume", number
                        ),
                    )
                )
            except ValueError as error:
                raise EtoroDemoError(f"invalid eToro candle {number}: {error}") from error
        return snapshots


def _find_object_list(
    payload: Mapping[str, Any], candidate_fields: Sequence[str]
) -> list[Mapping[str, Any]]:
    for field in candidate_fields:
        value = payload.get(field)
        if isinstance(value, list):
            if any(not isinstance(item, Mapping) for item in value):
                raise EtoroDemoError(f"eToro field {field} must contain objects")
            return value
        if isinstance(value, Mapping):
            try:
                return _find_object_list(value, candidate_fields)
            except EtoroDemoError:
                pass
    raise EtoroDemoError(
        "eToro response contains no recognised result list "
        f"(fields: {', '.join(sorted(str(key) for key in payload.keys()))})"
    )


def _pick(item: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in item:
            return item[name]
    return None


def _number(value: Any, field: str, candle: int) -> Decimal:
    if value is None or isinstance(value, bool):
        raise EtoroDemoError(f"candle {candle} field {field} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise EtoroDemoError(
            f"candle {candle} field {field} must be numeric"
        ) from error
    if not number.is_finite():
        raise EtoroDemoError(f"candle {candle} field {field} must be finite")
    return number


def _timestamp(value: Any, candle: int) -> datetime:
    if isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise EtoroDemoError(f"candle {candle} timestamp is invalid") from error
        if timestamp.tzinfo is None:
            raise EtoroDemoError(
                f"candle {candle} timestamp must include a UTC offset"
            )
        return timestamp.astimezone(UTC)

    # The candle API also emits Unix timestamps as JSON numbers. Accept seconds,
    # milliseconds, or microseconds by magnitude, but bound the resulting date
    # so a malformed number cannot be silently interpreted using the wrong unit.
    if value is None or isinstance(value, bool):
        raise EtoroDemoError(
            f"candle {candle} has no recognised timestamp field"
        )
    try:
        unix_value = Decimal(str(value))
        magnitude = abs(unix_value)
        divisor = (
            Decimal("1000000")
            if magnitude >= Decimal("100000000000000")
            else Decimal("1000")
            if magnitude >= Decimal("100000000000")
            else Decimal("1")
        )
        timestamp = datetime.fromtimestamp(float(unix_value / divisor), tz=UTC)
    except (InvalidOperation, ValueError, TypeError, OSError, OverflowError) as error:
        raise EtoroDemoError(f"candle {candle} timestamp is invalid") from error
    if not datetime(2000, 1, 1, tzinfo=UTC) <= timestamp <= datetime(
        2100, 1, 1, tzinfo=UTC
    ):
        raise EtoroDemoError(
            f"candle {candle} timestamp is outside the supported 2000–2100 range"
        )
    return timestamp


def _active_risk_event(
    decisions: Sequence[Decision], strategy_version: str, symbol: str
) -> ShadowRiskEvent | None:
    active: ShadowRiskEvent | None = None
    for decision in decisions:
        if decision.facts.get("manual_approval_on_candle") == "true":
            active = None
        if decision.facts.get("risk_triggered_on_candle") == "true":
            reasons = tuple(
                reason.strip()
                for reason in decision.facts.get("risk_reasons", "").split(";")
                if reason.strip()
            )
            evidence = {
                key: value
                for key, value in decision.facts.items()
                if key.startswith("risk_")
            }
            identity = "|".join(
                (strategy_version, symbol, decision.timestamp.isoformat(), *reasons)
            )
            active = ShadowRiskEvent(
                event_id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                triggered_at=decision.timestamp,
                reasons=reasons,
                evidence=evidence,
            )
    return active
