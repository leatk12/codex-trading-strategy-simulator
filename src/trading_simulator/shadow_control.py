"""Auditable local approvals and kill-switch state for eToro shadow mode."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .etoro_demo import EtoroDemoError
from .etoro_shadow import ShadowRiskEvent


@dataclass(frozen=True, slots=True)
class ShadowApproval:
    event_id: str
    effective_at: datetime
    approved_at: datetime
    approved_by: str


@dataclass(frozen=True, slots=True)
class ShadowControlState:
    kill_switch: bool = False
    kill_switch_reason: str | None = None
    changed_at: datetime | None = None
    changed_by: str | None = None
    approvals: tuple[ShadowApproval, ...] = ()

    @property
    def approval(self) -> ShadowApproval | None:
        return self.approvals[-1] if self.approvals else None

    @property
    def approval_times(self) -> tuple[datetime, ...]:
        if self.kill_switch:
            return ()
        return tuple(sorted({approval.effective_at for approval in self.approvals}))

    def approval_for(self, event: ShadowRiskEvent | None) -> datetime | None:
        if self.kill_switch or event is None:
            return None
        return next(
            (
                approval.effective_at
                for approval in self.approvals
                if approval.event_id == event.event_id
            ),
            None,
        )


class ShadowControlStore:
    SCHEMA_VERSION = 2

    def __init__(
        self, path: str | Path, *, event_log_path: str | Path | None = None
    ) -> None:
        self.path = Path(path)
        self.event_log_path = (
            None if event_log_path is None else Path(event_log_path)
        )

    def load(self) -> ShadowControlState:
        if not self.path.exists():
            return ShadowControlState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("control document must be an object")
            schema_version = raw.get("schema_version")
            if schema_version not in (1, self.SCHEMA_VERSION):
                raise ValueError("unsupported schema version")
            kill_switch = raw.get("kill_switch")
            if not isinstance(kill_switch, bool):
                raise ValueError("kill_switch must be boolean")
            approvals_raw = (
                raw.get("approvals", [])
                if schema_version == self.SCHEMA_VERSION
                else ([raw.get("approval")] if raw.get("approval") is not None else [])
            )
            if not isinstance(approvals_raw, list):
                raise ValueError("approvals must be a list")
            approvals: list[ShadowApproval] = []
            for approval_raw in approvals_raw:
                if not isinstance(approval_raw, Mapping):
                    raise ValueError("approval must be an object")
                approved_at = _datetime(approval_raw.get("approved_at"))
                approvals.append(
                    ShadowApproval(
                        event_id=_required_text(approval_raw, "event_id"),
                        effective_at=(
                            self._legacy_effective_at(
                                _required_text(approval_raw, "event_id"),
                                approved_at,
                            )
                            if schema_version == 1
                            else _datetime(approval_raw.get("effective_at"))
                        ),
                        approved_at=approved_at,
                        approved_by=_required_text(approval_raw, "approved_by"),
                    )
                )
            changed_at_raw = raw.get("changed_at")
            return ShadowControlState(
                kill_switch=kill_switch,
                kill_switch_reason=_optional_text(raw.get("kill_switch_reason")),
                changed_at=(
                    None if changed_at_raw is None else _datetime(changed_at_raw)
                ),
                changed_by=_optional_text(raw.get("changed_by")),
                approvals=tuple(approvals),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise EtoroDemoError(f"invalid shadow control file {self.path}: {error}") from error

    def _legacy_effective_at(
        self, event_id: str, fallback: datetime
    ) -> datetime:
        if self.event_log_path is None or not self.event_log_path.exists():
            return fallback
        try:
            with self.event_log_path.open("r", encoding="utf-8") as file:
                for line in file:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    event = raw.get("active_risk_event")
                    if (
                        isinstance(event, Mapping)
                        and event.get("event_id") == event_id
                    ):
                        return _datetime(event.get("triggered_at"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return fallback
        return fallback

    def approve(
        self, event: ShadowRiskEvent, *, approved_by: str, now: datetime | None = None
    ) -> ShadowControlState:
        actor = approved_by.strip()
        if not actor:
            raise EtoroDemoError("--approved-by must not be empty")
        current = self.load()
        if current.kill_switch:
            raise EtoroDemoError("kill switch is enabled; approval cannot override it")
        changed = _utc_now(now)
        retained = tuple(
            approval
            for approval in current.approvals
            if approval.event_id != event.event_id
        )
        state = ShadowControlState(
            kill_switch=False,
            changed_at=changed,
            changed_by=actor,
            approvals=retained
            + (ShadowApproval(event.event_id, event.triggered_at, changed, actor),),
        )
        self._save(state)
        return state

    def set_kill_switch(
        self,
        enabled: bool,
        *,
        changed_by: str,
        reason: str,
        now: datetime | None = None,
    ) -> ShadowControlState:
        actor = changed_by.strip()
        explanation = reason.strip()
        if not actor or not explanation:
            raise EtoroDemoError("kill-switch actor and reason must not be empty")
        current = self.load()
        state = ShadowControlState(
            kill_switch=enabled,
            kill_switch_reason=explanation,
            changed_at=_utc_now(now),
            changed_by=actor,
            # Enabling invalidates approval. Disabling never manufactures one.
            approvals=() if enabled else current.approvals,
        )
        self._save(state)
        return state

    def _save(self, state: ShadowControlState) -> None:
        document: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "kill_switch": state.kill_switch,
            "kill_switch_reason": state.kill_switch_reason,
            "changed_at": (
                None if state.changed_at is None else state.changed_at.isoformat()
            ),
            "changed_by": state.changed_by,
            "approvals": [
                {
                    "event_id": approval.event_id,
                    "effective_at": approval.effective_at.isoformat(),
                    "approved_at": approval.approved_at.isoformat(),
                    "approved_by": approval.approved_by,
                }
                for approval in state.approvals
            ],
        }
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise EtoroDemoError(f"could not save shadow control file: {error}") from error


def load_latest_risk_event(path: str | Path) -> ShadowRiskEvent:
    log_path = Path(path)
    try:
        last = ""
        with log_path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    last = line
        raw = json.loads(last)
        event = raw["active_risk_event"]
        if not isinstance(event, Mapping):
            raise ValueError("latest record has no active risk event")
        reasons_raw = event.get("reasons")
        evidence_raw = event.get("evidence")
        if not isinstance(reasons_raw, list) or not all(
            isinstance(reason, str) for reason in reasons_raw
        ):
            raise ValueError("risk reasons are invalid")
        if not isinstance(evidence_raw, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in evidence_raw.items()
        ):
            raise ValueError("risk evidence is invalid")
        return ShadowRiskEvent(
            event_id=_required_text(event, "event_id"),
            triggered_at=_datetime(event.get("triggered_at")),
            reasons=tuple(reasons_raw),
            evidence=dict(evidence_raw),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as error:
        raise EtoroDemoError(
            "latest shadow log record has no reviewable risk event; run the updated "
            "shadow loop for one cycle first"
        ) from error


def _utc_now(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise EtoroDemoError("control timestamp must include a timezone")
    return result.astimezone(UTC)


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be ISO-8601 text")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return result.astimezone(UTC)


def _required_text(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"{key} must not be empty")
    return result.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("optional text must be non-empty when present")
    return value.strip()
