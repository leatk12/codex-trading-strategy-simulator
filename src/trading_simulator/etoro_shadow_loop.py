"""Persistent audit recording for continuous, read-only eToro shadow mode."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .etoro_demo import EtoroDemoError
from .etoro_shadow import EtoroDryRunResult


@dataclass(frozen=True, slots=True)
class ShadowRecordOutcome:
    recorded: bool
    candle_timestamp: datetime
    path: Path


class EtoroShadowRecorder:
    """Append each newly completed candle decision once to a JSONL audit log."""

    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.exists() and self.path.is_dir():
            raise EtoroDemoError("shadow log path must be a file, not a directory")
        (
            self._latest,
            self._latest_has_risk_event,
            self._latest_risk_event_id,
        ) = self._read_latest_state()

    def record(
        self,
        result: EtoroDryRunResult,
        *,
        observed_at: datetime | None = None,
    ) -> ShadowRecordOutcome:
        candle_at = result.latest_candle.timestamp.astimezone(UTC)
        migration_record = (
            self._latest is not None
            and candle_at == self._latest
            and not self._latest_has_risk_event
        )
        risk_event_id = (
            None
            if result.active_risk_event is None
            else result.active_risk_event.event_id
        )
        risk_transition_record = (
            self._latest is not None
            and candle_at == self._latest
            and self._latest_has_risk_event
            and risk_event_id != self._latest_risk_event_id
        )
        if (
            self._latest is not None
            and candle_at <= self._latest
            and not migration_record
            and not risk_transition_record
        ):
            return ShadowRecordOutcome(False, candle_at, self.path)

        observed = observed_at or datetime.now(UTC)
        if observed.tzinfo is None:
            raise EtoroDemoError("shadow observation timestamp must include a timezone")
        decision = result.latest_decision
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "observed_at": observed.astimezone(UTC).isoformat(),
            "environment": "etoro_demo",
            "mode": "read_only_shadow",
            "strategy_version": result.strategy_version,
            "requested_symbol": result.requested_symbol,
            "profile_symbol": result.profile_symbol,
            "resolution": result.resolution.api_name,
            "completed_candle_count": result.completed_candle_count,
            "candle_timestamp": candle_at.isoformat(),
            "close": str(result.latest_candle.close),
            "market_state": decision.state.value,
            "decision": decision.action.value,
            "proposed_action": result.proposed_action,
            "proposed_cash_budget": (
                None
                if result.proposed_cash_budget is None
                else str(result.proposed_cash_budget)
            ),
            "reason": decision.reason,
            "facts": dict(decision.facts),
            "active_risk_event": (
                None
                if result.active_risk_event is None
                else {
                    "event_id": result.active_risk_event.event_id,
                    "triggered_at": result.active_risk_event.triggered_at.isoformat(),
                    "reasons": list(result.active_risk_event.reasons),
                    "evidence": dict(result.active_risk_event.evidence),
                }
            ),
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
            raise EtoroDemoError(f"could not append shadow log {self.path}: {error}") from error
        self._latest = candle_at
        self._latest_has_risk_event = True
        self._latest_risk_event_id = risk_event_id
        return ShadowRecordOutcome(True, candle_at, self.path)

    def _read_latest_state(self) -> tuple[datetime | None, bool, str | None]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None, False, None
        try:
            last_line = ""
            with self.path.open("r", encoding="utf-8") as file:
                for line in file:
                    if line.strip():
                        last_line = line
            if not last_line:
                return None, False, None
            record = json.loads(last_line)
            raw = record["candle_timestamp"]
            timestamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            has_risk_event = "active_risk_event" in record
            risk_event = record.get("active_risk_event")
            risk_event_id = (
                str(risk_event.get("event_id"))
                if isinstance(risk_event, dict) and risk_event.get("event_id")
                else None
            )
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as error:
            raise EtoroDemoError(
                f"existing shadow log has an invalid final record: {self.path}"
            ) from error
        if timestamp.tzinfo is None:
            raise EtoroDemoError(
                f"existing shadow log timestamp has no timezone: {self.path}"
            )
        return timestamp.astimezone(UTC), has_risk_event, risk_event_id
