"""Local dashboard for monitoring and explicitly armed Demo execution."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import base64
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Mapping
from threading import Event, Lock, Thread

from .etoro_demo import (
    EtoroCredentials,
    EtoroDemoError,
    EtoroDemoReadOnlyClient,
)
from .etoro_demo_execution import ARMING_PHRASE, ExecutionLedger, IntentAuditReader
from .etoro_live_state import EtoroLiveStateStore
from .shadow_control import ShadowControlStore, load_latest_risk_event
from .portfolio_risk import PortfolioRiskController, load_portfolio_risk_policy


ASSETS = ("btc", "eth", "sol", "xrp", "lac")
DASHBOARD_MAX_DEMO_ORDER_USD = Decimal("1000.00")
AUTOMATION_ARMING_PHRASE = "I_ENABLE_RULE_BASED_DEMO_AUTOMATION"
PASSWORD_HASH_ITERATIONS = 600_000
SESSION_COOKIE = "codex_trading_session"
SESSION_LIFETIME = timedelta(hours=8)


def hash_dashboard_password(password: str) -> str:
    """Return a salted password hash suitable for DASHBOARD_PASSWORD_HASH."""
    if len(password) < 12:
        raise ValueError("dashboard password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PASSWORD_HASH_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_dashboard_password(password: str, encoded: str) -> bool:
    """Verify a password without exposing malformed-hash details."""
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$")
        iterations = int(iterations_text)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def _write_http_body(stream: Any, body: bytes) -> None:
    """Write a response while tolerating a browser cancelling navigation."""
    try:
        stream.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return


class DashboardAuthenticator:
    """In-memory, expiring dashboard sessions backed by an environment hash."""

    def __init__(self, username: str, password_hash: str) -> None:
        username = username.strip()
        if not username or len(username) > 80:
            raise ValueError("DASHBOARD_USERNAME must contain 1 to 80 characters")
        parts = password_hash.split("$")
        try:
            valid_hash = (
                len(parts) == 4
                and parts[0] == "pbkdf2_sha256"
                and 100_000 <= int(parts[1]) <= 2_000_000
                and bool(base64.urlsafe_b64decode(parts[2].encode("ascii")))
                and bool(base64.urlsafe_b64decode(parts[3].encode("ascii")))
            )
        except (ValueError, UnicodeError):
            valid_hash = False
        if not valid_hash:
            raise ValueError("DASHBOARD_PASSWORD_HASH is malformed")
        self.username = username
        self.password_hash = password_hash
        self._sessions: dict[str, datetime] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = Lock()

    @classmethod
    def from_environment(cls) -> "DashboardAuthenticator":
        username = os.environ.get("DASHBOARD_USERNAME", "")
        password_hash = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
        if not username or not password_hash:
            raise ValueError(
                "set DASHBOARD_USERNAME and DASHBOARD_PASSWORD_HASH before starting the dashboard"
            )
        return cls(username, password_hash)

    def login(self, username: str, password: str, client: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            recent = [item for item in self._failures.get(client, []) if now - item < 60]
            self._failures[client] = recent
            if len(recent) >= 5:
                return None
        password_valid = verify_dashboard_password(password, self.password_hash)
        valid = secrets.compare_digest(username, self.username) and password_valid
        if not valid:
            with self._lock:
                self._failures.setdefault(client, []).append(now)
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._failures.pop(client, None)
            self._purge_expired()
            self._sessions[token] = datetime.now(UTC) + SESSION_LIFETIME
        return token

    def authenticated(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            self._purge_expired()
            return token in self._sessions

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def _purge_expired(self) -> None:
        now = datetime.now(UTC)
        self._sessions = {
            token: expiry for token, expiry in self._sessions.items() if expiry > now
        }


@dataclass(frozen=True, slots=True)
class AssetMonitorSpec:
    resolution: str
    stem: str
    audit_stem: str
    candle_count: int
    poll_seconds: int
    asset_class: str = "crypto"
    automation_allowed: bool = True


MONITOR_SPECS = {
    "btc": AssetMonitorSpec("one-hour", "btc-one-hour", "btc", 200, 300),
    "eth": AssetMonitorSpec("one-hour", "eth-one-hour", "eth", 200, 300),
    "sol": AssetMonitorSpec("one-hour", "sol-one-hour", "sol", 200, 300),
    "xrp": AssetMonitorSpec(
        "fifteen-minutes", "xrp-fifteen-minutes", "xrp-fifteen-minutes", 800, 60
    ),
    "lac": AssetMonitorSpec(
        "one-hour", "lac-one-hour", "lac", 500, 300,
        asset_class="equity", automation_allowed=False,
    ),
}


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    generated_at: str
    assets: tuple[Mapping[str, Any], ...]
    safety: Mapping[str, Any]
    portfolio_risk: Mapping[str, Any]
    automation: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "assets": [dict(asset) for asset in self.assets],
            "safety": dict(self.safety),
            "portfolio_risk": dict(self.portfolio_risk),
            "automation": dict(self.automation),
        }


class DashboardData:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)

    def snapshot(self) -> DashboardSnapshot:
        assets = tuple(self._asset(name) for name in ASSETS)
        automation = _automation_state(self.data_dir)
        return DashboardSnapshot(
            generated_at=datetime.now(UTC).isoformat(),
            assets=assets,
            safety={
                "environment": "eToro Demo",
                "mode": "supervised Demo dashboard",
                "order_execution": (
                    "rule-based eToro Demo automation"
                    if automation.get("enabled") is True
                    else "explicitly armed Demo only"
                ),
                "leverage": "1x",
                "borrowing_allowed": False,
                "real_account_access": "BLOCKED",
            },
            portfolio_risk=_document(
                self.data_dir / "portfolio-risk-state.json"
            ),
            automation=automation,
        )

    def _asset(self, name: str) -> Mapping[str, Any]:
        spec = MONITOR_SPECS[name]
        shadow = _last_json(self.data_dir / f"{spec.stem}.jsonl")
        readiness = _last_json(self.data_dir / f"{spec.audit_stem}-readiness.jsonl")
        control = _document(self.data_dir / f"{spec.stem}.control.json")
        intent_path = self.data_dir / f"{spec.audit_stem}-intents.jsonl"
        intent_count = _line_count(intent_path)
        latest_intent = _last_json(intent_path)
        latest_abandonment = _last_json(
            self.data_dir / f"{spec.audit_stem}-abandonments.jsonl"
        )
        latest_flat_rebaseline = _last_json(
            self.data_dir / f"{spec.audit_stem}-flat-rebaselines.jsonl"
        )
        live_state = _document(self.data_dir / f"{spec.stem}.live-state.json")
        ledger = ExecutionLedger(
            self.data_dir / f"{spec.audit_stem}-execution.jsonl"
        )
        ledger_path = self.data_dir / f"{spec.audit_stem}-execution.jsonl"
        latest_intent_id = latest_intent.get("intent_id")
        portfolio_risk = _document(self.data_dir / "portfolio-risk-state.json")
        reservations = portfolio_risk.get("reservations", {})
        reservation = (
            reservations.get(latest_intent_id, {})
            if isinstance(reservations, Mapping) and isinstance(latest_intent_id, str)
            else {}
        )
        reservation_active = bool(reservation)
        readiness_reason = _pick(readiness, shadow, "reason")
        candle_timestamp = _pick(readiness, shadow, "candle_timestamp")
        flat_rebaseline_applied = (
            readiness_reason
            == "Demo portfolio and simulator position state do not reconcile"
            and _iso_at_or_after(
                latest_flat_rebaseline.get("replacement_baseline"),
                candle_timestamp,
            )
        )
        error = next(
            (
                item["_error"]
                for item in (shadow, readiness, control)
                if isinstance(item, Mapping) and "_error" in item
            ),
            None,
        )
        try:
            execution = _execution_summary(ledger_path)
            intent_attempted = (
                ledger.has_attempt(latest_intent_id)
                if isinstance(latest_intent_id, str)
                else False
            )
        except EtoroDemoError as ledger_error:
            execution = {
                "status": "invalid",
                "pending": False,
                "intent_id": None,
            }
            intent_attempted = True
            error = error or str(ledger_error)
        risk = shadow.get("active_risk_event") if isinstance(shadow, Mapping) else None
        approved_ids = {
            approval.get("event_id")
            for approval in control.get("approvals", [])
            if isinstance(approval, Mapping)
        } if isinstance(control, Mapping) and isinstance(control.get("approvals", []), list) else set()
        risk_active = isinstance(risk, Mapping) and risk.get("event_id") not in approved_ids
        automation = _automation_state(self.data_dir)
        automation_review_required = (
            automation.get("enabled") is True
            and latest_intent.get("action") == "close-entire-long-position"
            and _pick(readiness, shadow, "market_state") == "explosive_momentum"
            and readiness.get("ready") is True
            and not intent_attempted
        )
        return {
            "asset": name.upper(),
            "asset_class": spec.asset_class,
            "automation_allowed": spec.automation_allowed,
            "resolution": spec.resolution,
            "available": bool(shadow or readiness),
            "error": error,
            "candle_timestamp": candle_timestamp,
            "observed_at": _pick(readiness, shadow, "observed_at"),
            "market_state": _pick(readiness, shadow, "market_state"),
            "decision": _pick(readiness, shadow, "decision"),
            "reason": (
                "Demo flat state confirmed; restart monitoring for the next evaluation."
                if flat_rebaseline_applied else readiness_reason
            ),
            "ready": (
                readiness.get("ready")
                if isinstance(readiness, Mapping)
                and latest_intent.get("action") != "close-entire-long-position"
                else False
            ),
            "intent_id": readiness.get("intent_id") if isinstance(readiness, Mapping) else None,
            "intent_count": intent_count,
            "intent_amount_usd": (
                latest_intent.get("request_body", {}).get("amount")
                if isinstance(latest_intent.get("request_body"), Mapping)
                else None
            ),
            "intent_action": latest_intent.get("action"),
            "reservation_active": reservation_active,
            "reservation_expires_at": (
                reservation.get("expires_at") if isinstance(reservation, Mapping) else None
            ),
            "holding_position": live_state.get("broker_position_id") is not None,
            "position_lots": len(
                live_state.get("broker_position_ids", [])
                if isinstance(live_state.get("broker_position_ids", []), list)
                else []
            ) or (1 if live_state.get("broker_position_id") is not None else 0),
            "position_allocation_usd": live_state.get("broker_amount_usd"),
            "scale_in_count": live_state.get("scale_in_count", 0),
            "execution_status": execution["status"],
            "reconciliation_pending": execution["pending"],
            "reconciliation_intent_id": execution["intent_id"],
            "can_resolve_rejected_close": (
                execution["pending"]
                and execution["status"] == "attempting"
                and execution["intent_id"] == latest_intent_id
                and latest_intent.get("action") == "close-entire-long-position"
            ),
            "can_execute_demo": (
                readiness.get("ready") is True
                and readiness.get("intent_id") == latest_intent_id
                and latest_intent.get("environment") == "etoro_demo"
                and latest_intent.get("execution_eligible") is True
                and latest_intent.get("order_submitted") is False
                and not intent_attempted
                and latest_intent_id != latest_abandonment.get("intent_id")
                and (
                    latest_intent.get("action") == "close-entire-long-position"
                    or reservation_active
                )
            ),
            "can_rebaseline": (
                intent_count > 0
                and latest_intent.get("action") == "open-long-by-cash-amount"
                and latest_intent.get("environment") == "etoro_demo"
                and latest_intent.get("execution_eligible") is True
                and latest_intent.get("order_submitted") is False
                and not intent_attempted
                and latest_intent.get("intent_id")
                != latest_abandonment.get("intent_id")
            ),
            "can_dismiss_intent": (
                latest_intent.get("action") in {
                    "add-long-by-cash-amount",
                    "close-entire-long-position",
                }
                and latest_intent.get("execution_eligible") is True
                and not intent_attempted
                and latest_intent_id != latest_abandonment.get("intent_id")
                and live_state.get("last_abandoned_intent_id") != latest_intent_id
            ),
            "can_flat_rebaseline": (
                not flat_rebaseline_applied
                and live_state.get("broker_position_id") is None
                and readiness_reason
                == "Demo portfolio and simulator position state do not reconcile"
                and not (
                    latest_intent.get("action") == "open-long-by-cash-amount"
                    and latest_intent.get("execution_eligible") is True
                    and latest_intent.get("intent_id")
                    != latest_abandonment.get("intent_id")
                )
                and not execution["pending"]
            ),
            "flat_rebaseline_applied": flat_rebaseline_applied,
            "risk_active": risk_active,
            "risk_event_id": risk.get("event_id") if isinstance(risk, Mapping) else None,
            "kill_switch": control.get("kill_switch", False) if isinstance(control, Mapping) else False,
            "automation_review_required": automation_review_required,
            "automation_review_reason": (
                "Unusual rapid appreciation preceded this exit; confirm whether to apply the configured trailing sell rule."
                if automation_review_required else None
            ),
            "leverage": 1,
            "order_submitted": False,
        }


class DashboardActions:
    """Apply local controls and the separately armed Demo-only write path."""

    def __init__(self, data_dir: str | Path, project_dir: str | Path = ".") -> None:
        self.data_dir = Path(data_dir)
        self.project_dir = Path(project_dir).resolve()
        self._lock = Lock()

    def set_portfolio_kill_switch(
        self, enabled: bool, actor: str, reason: str
    ) -> Mapping[str, Any]:
        operator = _actor(actor)
        if not reason.strip():
            raise EtoroDemoError("portfolio kill-switch reason is required")
        with self._lock:
            state = self._portfolio_risk_controller().set_manual_kill_switch(
                enabled, changed_by=operator, reason=reason
            )
            _append_jsonl(
                self.data_dir / "portfolio-risk-controls.jsonl",
                {
                    "schema_version": 1,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "action": "kill_switch_enabled" if enabled else "kill_switch_disabled",
                    "changed_by": operator,
                    "reason": reason.strip(),
                    "buying_halted": state.buying_halted,
                    "order_submitted": False,
                    "leverage": 1,
                },
            )
        return {
            "ok": True,
            "action": "portfolio_kill_switch_changed",
            "buying_halted": state.buying_halted,
            "order_submitted": False,
        }

    def reset_portfolio_risk_halt(
        self, actor: str, reason: str
    ) -> Mapping[str, Any]:
        operator = _actor(actor)
        if not reason.strip():
            raise EtoroDemoError("portfolio risk-reset reason is required")
        with self._lock:
            state = self._portfolio_risk_controller().reset_risk_halt(
                changed_by=operator, reason=reason
            )
            _append_jsonl(
                self.data_dir / "portfolio-risk-controls.jsonl",
                {
                    "schema_version": 1,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "action": "risk_halt_reset",
                    "changed_by": operator,
                    "reason": reason.strip(),
                    "buying_halted": state.buying_halted,
                    "order_submitted": False,
                    "leverage": 1,
                },
            )
        return {
            "ok": True,
            "action": "portfolio_risk_halt_reset",
            "buying_halted": state.buying_halted,
            "order_submitted": False,
        }

    def _portfolio_risk_controller(self) -> PortfolioRiskController:
        return PortfolioRiskController(
            load_portfolio_risk_policy(
                self.project_dir / "configs" / "portfolio_risk.toml"
            ),
            self.data_dir / "portfolio-risk-state.json",
        )

    def set_demo_automation(
        self, enabled: bool, actor: str, acknowledgement: str
    ) -> Mapping[str, Any]:
        operator = _actor(actor)
        if enabled and acknowledgement != AUTOMATION_ARMING_PHRASE:
            raise EtoroDemoError("the Demo automation acknowledgement does not match")
        with self._lock:
            current = _automation_state(self.data_dir)
            state = {
                "schema_version": 1,
                "enabled": enabled,
                "changed_at": datetime.now(UTC).isoformat(),
                "changed_by": operator,
                "environment": "etoro_demo",
                "routine_actions_automatic": enabled,
                "unusual_drop_requires_approval": True,
                "unusual_climb_sell_requires_approval": True,
                "real_account_allowed": False,
                "leverage": 1,
            }
            _write_json(self.data_dir / "demo-automation-state.json", state)
            _append_jsonl(
                self.data_dir / "demo-automation-audit.jsonl",
                {
                    **state,
                    "action": "automation_enabled" if enabled else "automation_disabled",
                    "previously_enabled": current.get("enabled") is True,
                    "order_submitted": False,
                },
            )
        return {"ok": True, **state, "order_submitted": False}

    def execute_demo_intent(
        self,
        asset: str,
        intent_id: str,
        actor: str,
        arming_phrase: str,
        max_order_usd: str,
    ) -> Mapping[str, Any]:
        # Serialize all dashboard writes so two asset buttons cannot race the
        # shared Demo cash balance or each other's live portfolio snapshot.
        with self._lock:
            return self._execute_demo_intent_unlocked(
                asset, intent_id, actor, arming_phrase, max_order_usd
            )

    def _execute_demo_intent_unlocked(
        self,
        asset: str,
        intent_id: str,
        actor: str,
        arming_phrase: str,
        max_order_usd: str,
    ) -> Mapping[str, Any]:
        clean = asset.strip().lower()
        if clean not in ASSETS:
            raise EtoroDemoError("unknown dashboard asset")
        operator = _actor(actor)
        if arming_phrase != ARMING_PHRASE:
            raise EtoroDemoError("the Demo execution arming phrase does not match")
        try:
            cap = Decimal(max_order_usd)
        except (InvalidOperation, ValueError) as error:
            raise EtoroDemoError("maximum Demo order amount is invalid") from error
        if not cap.is_finite() or cap <= 0 or cap > DASHBOARD_MAX_DEMO_ORDER_USD:
            raise EtoroDemoError(
                f"maximum Demo order amount must be between 0 and {DASHBOARD_MAX_DEMO_ORDER_USD} USD"
            )
        spec = MONITOR_SPECS[clean]
        latest = _last_json(self.data_dir / f"{spec.audit_stem}-intents.jsonl")
        if latest.get("intent_id") != intent_id:
            raise EtoroDemoError("intent changed; refresh before executing")
        if (
            latest.get("environment") != "etoro_demo"
            or latest.get("execution_eligible") is not True
            or latest.get("order_submitted") is not False
        ):
            raise EtoroDemoError("intent is not eligible for Demo execution")
        ledger_path = self.data_dir / f"{spec.audit_stem}-execution.jsonl"
        if _execution_summary(ledger_path)["pending"]:
            raise EtoroDemoError(
                "an earlier Demo execution requires reconciliation; no new order is allowed"
            )
        ExecutionLedger(ledger_path).assert_not_attempted(intent_id)
        command = [
            sys.executable, "-m", "trading_simulator", "etoro-demo-execute-intent",
            "--config", str(self.project_dir / "configs" / f"{clean}_example.toml"),
            "--symbol", clean.upper(), "--resolution", spec.resolution,
            "--candles", str(spec.candle_count),
            "--shadow-log", str(self.data_dir / f"{spec.stem}.jsonl"),
            "--live-state", str(self.data_dir / f"{spec.stem}.live-state.json"),
            "--intent-log", str(self.data_dir / f"{spec.audit_stem}-intents.jsonl"),
            "--intent-id", intent_id,
            "--execution-ledger", str(ledger_path),
            "--minimum-order-usd", "10.00", "--amount-increment-usd", "0.01",
            "--max-demo-order-usd", str(cap),
            "--portfolio-risk-config",
            str(self.project_dir / "configs" / "portfolio_risk.toml"),
            "--portfolio-risk-state",
            str(self.data_dir / "portfolio-risk-state.json"),
            "--arm-demo-execution", ARMING_PHRASE,
        ]
        _append_jsonl_once(
            self.data_dir / f"{spec.audit_stem}-execution-approvals.jsonl",
            intent_id,
            {
                "schema_version": 1,
                "intent_id": intent_id,
                "approved_at": datetime.now(UTC).isoformat(),
                "approved_by": operator,
                "maximum_order_usd": str(cap),
                "environment": "etoro_demo",
                "leverage": 1,
                "real_account_allowed": False,
            },
        )
        try:
            completed = subprocess.run(
                command,
                cwd=self.project_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EtoroDemoError(
                "Demo submission outcome may be uncertain; do not retry and reconcile the portfolio"
            ) from error
        if completed.returncode != 0:
            message = _last_cli_error(completed.stdout)
            if "returned HTTP 4" in message:
                ledger.record_rejection(
                    intent_id,
                    reason=message,
                    confirmed_by="execution-client",
                )
            raise EtoroDemoError(message)
        return {
            "ok": True,
            "action": "demo_order_submitted",
            "intent_id": intent_id,
            "environment": "etoro_demo",
            "leverage": 1,
            "real_account_allowed": False,
            "next_step": "reconcile the Demo position; do not submit again",
        }

    def reconcile_demo_execution(
        self, asset: str, intent_id: str
    ) -> Mapping[str, Any]:
        with self._lock:
            clean = asset.strip().lower()
            if clean not in ASSETS:
                raise EtoroDemoError("unknown dashboard asset")
            spec = MONITOR_SPECS[clean]
            ledger_path = self.data_dir / f"{spec.audit_stem}-execution.jsonl"
            execution = _execution_summary(ledger_path)
            if not execution["pending"] or execution["intent_id"] != intent_id:
                raise EtoroDemoError(
                    "execution no longer awaits reconciliation; refresh the dashboard"
                )
            command = [
                sys.executable, "-m", "trading_simulator",
                "etoro-demo-reconcile-execution",
                "--intent-log",
                str(self.data_dir / f"{spec.audit_stem}-intents.jsonl"),
                "--intent-id", intent_id,
                "--execution-ledger", str(ledger_path),
                "--live-state", str(self.data_dir / f"{spec.stem}.live-state.json"),
                "--portfolio-risk-config",
                str(self.project_dir / "configs" / "portfolio_risk.toml"),
                "--portfolio-risk-state",
                str(self.data_dir / "portfolio-risk-state.json"),
                "--poll-seconds", "5", "--timeout-seconds", "60",
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=self.project_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=75,
                    check=False,
                )
            except OSError as error:
                raise EtoroDemoError(
                    "could not start read-only Demo reconciliation"
                ) from error
            except subprocess.TimeoutExpired as error:
                raise EtoroDemoError(
                    "reconciliation timed out; no order was submitted or retried"
                ) from error
            if completed.returncode != 0:
                raise EtoroDemoError(_last_cli_error(completed.stdout))
            state = EtoroLiveStateStore(
                self.data_dir / f"{spec.stem}.live-state.json"
            ).load()
            return {
                "ok": True,
                "action": "demo_execution_reconciled",
                "intent_id": intent_id,
                "position_open": (
                    state is not None and state.broker_position_id is not None
                ),
                "order_submitted": False,
                "order_retried": False,
                "environment": "etoro_demo",
                "leverage": 1,
            }

    def approve(self, asset: str, event_id: str, actor: str) -> Mapping[str, Any]:
        shadow_path, control_path = self._paths(asset)
        event = load_latest_risk_event(shadow_path)
        if event.event_id != event_id:
            raise EtoroDemoError("risk event changed; refresh before approving")
        state = ShadowControlStore(
            control_path, event_log_path=shadow_path
        ).approve(event, approved_by=_actor(actor))
        return {
            "ok": True,
            "action": "approved",
            "event_id": event.event_id,
            "kill_switch": state.kill_switch,
                "order_submitted": False,
            }

    def resolve_rejected_close(
        self,
        asset: str,
        intent_id: str,
        actor: str,
        acknowledgement: str,
    ) -> Mapping[str, Any]:
        if acknowledgement != "I_SAW_HTTP_400_REJECTION":
            raise EtoroDemoError("HTTP 400 acknowledgement phrase does not match")
        clean = asset.strip().lower()
        if clean not in ASSETS:
            raise EtoroDemoError("unknown dashboard asset")
        operator = _actor(actor)
        spec = MONITOR_SPECS[clean]
        with self._lock:
            ledger = ExecutionLedger(
                self.data_dir / f"{spec.audit_stem}-execution.jsonl"
            )
            execution = _execution_summary(ledger.path)
            if (
                not execution["pending"]
                or execution["status"] != "attempting"
                or execution["intent_id"] != intent_id
            ):
                raise EtoroDemoError("close attempt is not awaiting rejection resolution")
            audited = IntentAuditReader(
                self.data_dir / f"{spec.audit_stem}-intents.jsonl"
            ).load(intent_id)
            if audited.action != "close-entire-long-position":
                raise EtoroDemoError("intent is not a full-position close")
            position_id = int(audited.request_path_template.rsplit("/", 1)[-1])
            live_state = EtoroLiveStateStore(
                self.data_dir / f"{spec.stem}.live-state.json"
            ).load()
            if live_state is None or live_state.broker_position_id != position_id:
                raise EtoroDemoError("live state no longer matches the rejected close")
            client = EtoroDemoReadOnlyClient(EtoroCredentials.from_environment())
            pnl = client.demo_pnl()
            summary = client.demo_summary(pnl)
            portfolio = pnl.get("clientPortfolio")
            if not isinstance(portfolio, Mapping):
                raise EtoroDemoError("eToro P&L response is missing clientPortfolio")
            positions = portfolio.get("positions", [])
            if not isinstance(positions, list) or any(
                not isinstance(item, Mapping) for item in positions
            ):
                raise EtoroDemoError("Demo positions are invalid")
            matches = [
                item for item in positions
                if _dashboard_int(item.get("positionID", item.get("positionId")))
                == position_id
                and _dashboard_int(item.get("instrumentID", item.get("instrumentId")))
                == live_state.instrument_id
            ]
            if len(matches) != 1:
                raise EtoroDemoError(
                    "exact XRP Demo position is not still open; use read-only reconciliation"
                )
            position = matches[0]
            if position.get("isBuy", position.get("IsBuy")) is not True:
                raise EtoroDemoError("remaining Demo position is not long")
            if _dashboard_int(position.get("leverage", position.get("Leverage"))) != 1:
                raise EtoroDemoError("remaining Demo position is not 1x")
            if summary.pending_order_count or portfolio.get("mirrors", []):
                raise EtoroDemoError(
                    "a pending order or copy exposure exists; rejection cannot be resolved"
                )
            ledger.record_rejection(
                intent_id,
                reason=(
                    "operator observed HTTP 400 InstrumentId validation rejection; "
                    "read-only Demo check confirmed the exact position remains open"
                ),
                confirmed_by=operator,
            )
        return {
            "ok": True,
            "action": "rejected_close_resolved",
            "intent_id": intent_id,
            "position_still_open": True,
            "order_submitted": False,
        }

    def refuse(self, asset: str, event_id: str, actor: str) -> Mapping[str, Any]:
        shadow_path, control_path = self._paths(asset)
        event = load_latest_risk_event(shadow_path)
        if event.event_id != event_id:
            raise EtoroDemoError("risk event changed; refresh before refusing")
        state = ShadowControlStore(
            control_path, event_log_path=shadow_path
        ).set_kill_switch(
            True,
            changed_by=_actor(actor),
            reason=f"risk event {event.event_id} refused from local dashboard",
        )
        return {
            "ok": True,
            "action": "refused_and_halted",
            "event_id": event.event_id,
            "kill_switch": state.kill_switch,
            "order_submitted": False,
        }

    def reenable(self, asset: str, actor: str) -> Mapping[str, Any]:
        shadow_path, control_path = self._paths(asset)
        store = ShadowControlStore(control_path, event_log_path=shadow_path)
        if not store.load().kill_switch:
            raise EtoroDemoError("kill switch is already disabled")
        state = store.set_kill_switch(
            False,
            changed_by=_actor(actor),
            reason="operator re-enabled monitoring from local dashboard",
        )
        return {
            "ok": True,
            "action": "monitoring_reenabled",
            "kill_switch": state.kill_switch,
            "order_submitted": False,
        }

    def dismiss_unexecuted_intent(
        self, asset: str, intent_id: str, actor: str
    ) -> Mapping[str, Any]:
        clean = asset.strip().lower()
        if clean not in ASSETS:
            raise EtoroDemoError("unknown dashboard asset")
        operator = _actor(actor)
        spec = MONITOR_SPECS[clean]
        with self._lock:
            intent = _last_json(
                self.data_dir / f"{spec.audit_stem}-intents.jsonl"
            )
            if intent.get("intent_id") != intent_id:
                raise EtoroDemoError("intent changed; refresh before dismissing")
            if intent.get("action") not in {
                "add-long-by-cash-amount",
                "close-entire-long-position",
            }:
                raise EtoroDemoError("this intent cannot be dismissed without rebaselining")
            ExecutionLedger(
                self.data_dir / f"{spec.audit_stem}-execution.jsonl"
            ).assert_not_attempted(intent_id)
            _append_jsonl_once(
                self.data_dir / f"{spec.audit_stem}-abandonments.jsonl",
                intent_id,
                {
                    "schema_version": 1,
                    "intent_id": intent_id,
                    "strategy_version": intent.get("strategy_version"),
                    "asset": clean.upper(),
                    "abandoned_at": datetime.now(UTC).isoformat(),
                    "abandoned_by": operator,
                    "reason": "operator dismissed unexecuted trigger; monitoring continues",
                    "environment": "etoro_demo",
                    "order_submitted": False,
                    "leverage": 1,
                },
            )
            EtoroLiveStateStore(
                self.data_dir / f"{spec.stem}.live-state.json"
            ).record_abandoned_intent(intent_id)
            self._portfolio_risk_controller().release_reservation(
                intent_id,
                changed_by=operator,
                reason="unexecuted trigger dismissed while monitoring continues",
            )
        return {
            "ok": True,
            "action": "intent_dismissed",
            "intent_id": intent_id,
            "monitoring_continues": True,
            "order_submitted": False,
        }

    def abandon_and_rebaseline(
        self, asset: str, actor: str
    ) -> Mapping[str, Any]:
        clean = asset.strip().lower()
        if clean not in ASSETS:
            raise EtoroDemoError("unknown dashboard asset")
        operator = _actor(actor)
        spec = MONITOR_SPECS[clean]
        with self._lock:
            intent = _last_json(
                self.data_dir / f"{spec.audit_stem}-intents.jsonl"
            )
            if (
                intent.get("environment") != "etoro_demo"
                or intent.get("execution_eligible") is not True
                or intent.get("order_submitted") is not False
            ):
                raise EtoroDemoError("no abandonable Demo intent exists")
            intent_id = intent.get("intent_id")
            if not isinstance(intent_id, str) or not intent_id:
                raise EtoroDemoError("latest intent ID is invalid")
            ledger = ExecutionLedger(
                self.data_dir / f"{spec.audit_stem}-execution.jsonl"
            )
            if ledger.has_attempt(intent_id):
                raise EtoroDemoError(
                    "intent has an execution attempt and cannot be abandoned"
                )
            client = EtoroDemoReadOnlyClient(EtoroCredentials.from_environment())
            pnl = client.demo_pnl()
            summary = client.demo_summary(pnl)
            portfolio = pnl.get("clientPortfolio")
            if not isinstance(portfolio, Mapping):
                raise EtoroDemoError("eToro P&L response is missing clientPortfolio")
            state = EtoroLiveStateStore(
                self.data_dir / f"{spec.stem}.live-state.json"
            ).load()
            if state is None:
                raise EtoroDemoError("live-state checkpoint does not exist")
            positions = portfolio.get("positions", [])
            if not isinstance(positions, list) or any(
                not isinstance(item, Mapping) for item in positions
            ):
                raise EtoroDemoError("Demo positions are invalid")
            matching_positions = [
                item for item in positions
                if _dashboard_int(
                    item.get("instrumentID", item.get("instrumentId"))
                ) == state.instrument_id
            ]
            if (
                matching_positions
                or summary.pending_order_count != 0
                or portfolio.get("mirrors", [])
            ):
                raise EtoroDemoError(
                    "this Demo instrument is not flat or an order is pending; rebaseline refused"
                )
            shadow = _last_json(self.data_dir / f"{spec.stem}.jsonl")
            raw_timestamp = shadow.get("candle_timestamp")
            if not isinstance(raw_timestamp, str):
                raise EtoroDemoError("latest completed candle is unavailable")
            try:
                baseline = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            except ValueError as error:
                raise EtoroDemoError("latest candle timestamp is invalid") from error
            if baseline.tzinfo is None:
                raise EtoroDemoError("latest candle timestamp has no timezone")
            audit_path = self.data_dir / f"{spec.audit_stem}-abandonments.jsonl"
            _append_jsonl_once(
                audit_path,
                intent_id,
                {
                    "schema_version": 1,
                    "intent_id": intent_id,
                    "strategy_version": intent.get("strategy_version"),
                    "asset": clean.upper(),
                    "abandoned_at": datetime.now(UTC).isoformat(),
                    "abandoned_by": operator,
                    "reason": "operator rejected unexecuted intent; Demo confirmed flat",
                    "replacement_baseline": baseline.astimezone(UTC).isoformat(),
                    "environment": "etoro_demo",
                    "order_submitted": False,
                    "leverage": 1,
                },
            )
            state = EtoroLiveStateStore(
                self.data_dir / f"{spec.stem}.live-state.json"
            ).rebaseline_flat(
                baseline=baseline,
                abandoned_intent_id=intent_id,
            )
            self._portfolio_risk_controller().release_reservation(
                intent_id,
                changed_by=operator,
                reason="unexecuted intent abandoned and asset rebaselined",
            )
        return {
            "ok": True,
            "action": "intent_abandoned_and_rebaselined",
            "intent_id": intent_id,
            "generation": state.generation,
            "baseline": state.baseline_candle_timestamp,
            "order_submitted": False,
        }

    def confirm_flat_and_rebaseline(
        self, asset: str, actor: str
    ) -> Mapping[str, Any]:
        clean = asset.strip().lower()
        if clean not in ASSETS:
            raise EtoroDemoError("unknown dashboard asset")
        operator = _actor(actor)
        spec = MONITOR_SPECS[clean]
        with self._lock:
            execution = _execution_summary(
                self.data_dir / f"{spec.audit_stem}-execution.jsonl"
            )
            if execution["pending"]:
                raise EtoroDemoError(
                    "reconcile the outstanding Demo execution before rebaselining"
                )
            live_store = EtoroLiveStateStore(
                self.data_dir / f"{spec.stem}.live-state.json"
            )
            live_state = live_store.load()
            if live_state is None:
                raise EtoroDemoError("live-state checkpoint does not exist")
            client = EtoroDemoReadOnlyClient(EtoroCredentials.from_environment())
            pnl = client.demo_pnl()
            summary = client.demo_summary(pnl)
            portfolio = pnl.get("clientPortfolio")
            if not isinstance(portfolio, Mapping):
                raise EtoroDemoError("eToro P&L response is missing clientPortfolio")
            positions = portfolio.get("positions", [])
            if not isinstance(positions, list) or any(
                not isinstance(item, Mapping) for item in positions
            ):
                raise EtoroDemoError("Demo positions are invalid")
            matching = [
                item for item in positions
                if _dashboard_int(item.get("instrumentID", item.get("instrumentId")))
                == live_state.instrument_id
            ]
            if matching:
                raise EtoroDemoError(
                    "Demo has an open position for this asset; flat rebaseline refused"
                )
            if summary.pending_order_count or portfolio.get("mirrors", []):
                raise EtoroDemoError(
                    "Demo has a pending order or copy exposure; flat rebaseline refused"
                )
            shadow = _last_json(self.data_dir / f"{spec.stem}.jsonl")
            raw_timestamp = shadow.get("candle_timestamp")
            if not isinstance(raw_timestamp, str):
                raise EtoroDemoError("latest completed candle is unavailable")
            try:
                baseline = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            except ValueError as error:
                raise EtoroDemoError("latest candle timestamp is invalid") from error
            if baseline.tzinfo is None:
                raise EtoroDemoError("latest candle timestamp has no timezone")
            _append_jsonl(
                self.data_dir / f"{spec.audit_stem}-flat-rebaselines.jsonl",
                {
                    "schema_version": 1,
                    "asset": clean.upper(),
                    "instrument_id": live_state.instrument_id,
                    "confirmed_at": datetime.now(UTC).isoformat(),
                    "confirmed_by": operator,
                    "replacement_baseline": baseline.astimezone(UTC).isoformat(),
                    "reason": "Demo instrument confirmed flat after replay mismatch",
                    "other_demo_positions_preserved": len(positions),
                    "order_submitted": False,
                    "leverage": 1,
                },
            )
            updated = live_store.rebaseline_flat(baseline=baseline)
        return {
            "ok": True,
            "action": "demo_flat_confirmed_and_rebaselined",
            "generation": updated.generation,
            "baseline": updated.baseline_candle_timestamp,
            "order_submitted": False,
        }

    def _paths(self, asset: str) -> tuple[Path, Path]:
        clean = asset.strip().lower()
        if clean not in ASSETS:
            raise EtoroDemoError("unknown dashboard asset")
        spec = MONITOR_SPECS[clean]
        return (
            self.data_dir / f"{spec.stem}.jsonl",
            self.data_dir / f"{spec.stem}.control.json",
        )


class DashboardMonitorManager:
    """Own dashboard-started monitors and stop them with the dashboard."""

    def __init__(self, project_dir: str | Path, data_dir: str | Path) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        self._processes: dict[str, tuple[subprocess.Popen[bytes], Any]] = {}
        self._last_errors: dict[str, str] = {}
        self._resume_after_reconciliation: set[str] = set()

    def running(self, asset: str) -> bool:
        clean = asset.lower()
        item = self._processes.get(clean)
        if item is None:
            return False
        process, output = item
        if process.poll() is None:
            return True
        output.close()
        self._processes.pop(clean, None)
        spec = MONITOR_SPECS.get(clean)
        if spec is not None:
            message = _last_monitor_halt(
                self.data_dir / f"{spec.stem}-dashboard-monitor.log"
            )
            self._last_errors[clean] = message or (
                f"Monitor exited unexpectedly (exit code {process.returncode})."
            )
        return False

    def last_error(self, asset: str) -> str | None:
        return self._last_errors.get(asset.strip().lower())

    def clear_error(self, asset: str) -> None:
        self._last_errors.pop(asset.strip().lower(), None)

    def start(self, asset: str) -> Mapping[str, Any]:
        clean = asset.strip().lower()
        if clean not in ASSETS:
            raise EtoroDemoError("unknown dashboard asset")
        if self.running(clean):
            raise EtoroDemoError(f"{clean.upper()} monitoring is already running")
        self._last_errors.pop(clean, None)
        if not os.environ.get("ETORO_PUBLIC_KEY") or not os.environ.get("ETORO_PRIVATE_KEY"):
            raise EtoroDemoError(
                "dashboard process has no eToro keys; restart it from a PowerShell window where both keys are loaded"
            )
        config = self.project_dir / "configs" / f"{clean}_example.toml"
        if not config.is_file():
            raise EtoroDemoError(f"missing asset config {config.name}")
        spec = MONITOR_SPECS[clean]
        execution = _execution_summary(
            self.data_dir / f"{spec.audit_stem}-execution.jsonl"
        )
        if execution["pending"]:
            raise EtoroDemoError(
                "reconcile the outstanding Demo execution before restarting monitoring"
            )
        control = ShadowControlStore(
            self.data_dir / f"{spec.stem}.control.json",
            event_log_path=self.data_dir / f"{spec.stem}.jsonl",
        ).load()
        if control.kill_switch:
            raise EtoroDemoError("disable the asset kill switch before starting")
        command = [
            sys.executable, "-u", "-m", "trading_simulator", "etoro-readiness-loop",
            "--config", str(config), "--symbol", clean.upper(),
            "--resolution", spec.resolution, "--candles", str(spec.candle_count),
            "--shadow-log", str(self.data_dir / f"{spec.stem}.jsonl"),
            "--readiness-log", str(self.data_dir / f"{spec.audit_stem}-readiness.jsonl"),
            "--intent-log", str(self.data_dir / f"{spec.audit_stem}-intents.jsonl"),
            "--execution-ledger", str(
                self.data_dir / f"{spec.audit_stem}-execution.jsonl"
            ),
            "--minimum-order-usd", "10.00", "--amount-increment-usd", "0.01",
            "--poll-seconds", str(spec.poll_seconds),
            "--portfolio-risk-config",
            str(self.project_dir / "configs" / "portfolio_risk.toml"),
            "--portfolio-risk-state",
            str(self.data_dir / "portfolio-risk-state.json"),
        ]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        output = (self.data_dir / f"{spec.stem}-dashboard-monitor.log").open("ab")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            process = subprocess.Popen(
                command, cwd=self.project_dir, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
            )
        except OSError as error:
            output.close()
            raise EtoroDemoError(f"could not start monitor: {error}") from error
        self._processes[clean] = (process, output)
        return {
            "ok": True, "action": "monitor_started", "asset": clean.upper(),
            "pid": process.pid, "order_submitted": False,
        }

    def stop(self, asset: str) -> Mapping[str, Any]:
        clean = asset.strip().lower()
        if clean not in ASSETS:
            raise EtoroDemoError("unknown dashboard asset")
        item = self._processes.get(clean)
        if item is None or not self.running(clean):
            raise EtoroDemoError(f"{clean.upper()} monitoring is not running")
        process, output = self._processes.pop(clean)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        output.close()
        self._last_errors.pop(clean, None)
        return {
            "ok": True,
            "action": "monitor_stopped",
            "asset": clean.upper(),
            "order_submitted": False,
        }

    def pause_for_execution(self, asset: str) -> bool:
        clean = asset.strip().lower()
        if not self.running(clean):
            return False
        self.stop(clean)
        self._resume_after_reconciliation.add(clean)
        return True

    def resume_after_reconciliation(self, asset: str) -> bool:
        clean = asset.strip().lower()
        if clean not in self._resume_after_reconciliation:
            return False
        self.start(clean)
        self._resume_after_reconciliation.remove(clean)
        return True

    def resume_after_failed_execution_if_safe(self, asset: str) -> bool:
        clean = asset.strip().lower()
        if clean not in self._resume_after_reconciliation:
            return False
        spec = MONITOR_SPECS[clean]
        if _execution_summary(
            self.data_dir / f"{spec.audit_stem}-execution.jsonl"
        )["pending"]:
            return False
        return self.resume_after_reconciliation(clean)

    def stop_all(self) -> None:
        for process, output in tuple(self._processes.values()):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            output.close()
        self._processes.clear()
        self._resume_after_reconciliation.clear()


class DemoAutomationCoordinator:
    """Execute only routine, pre-audited eToro Demo intents.

    Structural-breakdown events are stopped by the intent builder.  A close
    produced while the asset is in EXPLOSIVE_MOMENTUM is deliberately left for
    an operator because the initial specification treats that regime as
    unusual.  The execution ledger remains the final no-retry boundary.
    """

    def __init__(
        self,
        source: DashboardData,
        actions: DashboardActions,
        monitors: DashboardMonitorManager,
        data_dir: str | Path,
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        self.source = source
        self.actions = actions
        self.monitors = monitors
        self.data_dir = Path(data_dir)
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._tick_lock = Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(
            target=self._run,
            name="demo-automation-coordinator",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval_seconds + 1, 6))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.tick()
            except Exception as error:  # keep dashboard alive; audit only the type
                _append_jsonl(
                    self.data_dir / "demo-automation-audit.jsonl",
                    {
                        "schema_version": 1,
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "action": "coordinator_error",
                        "error_type": type(error).__name__,
                        "order_retry_performed": False,
                        "real_account_allowed": False,
                        "leverage": 1,
                    },
                )

    def tick(self) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            if _automation_state(self.data_dir).get("enabled") is not True:
                return
            for asset in self.source.snapshot().assets:
                self._process_asset(asset)
        finally:
            self._tick_lock.release()

    def _process_asset(self, asset: Mapping[str, Any]) -> None:
        name = str(asset.get("asset", "")).lower()
        intent_id = asset.get("reconciliation_intent_id")
        if asset.get("reconciliation_pending") is True and isinstance(intent_id, str):
            if self.monitors.running(name):
                self.monitors.pause_for_execution(name)
            try:
                result = self.actions.reconcile_demo_execution(name, intent_id)
                self.monitors.resume_after_reconciliation(name)
                self._audit(name, intent_id, "automatic_reconciliation_complete", result)
            except EtoroDemoError as error:
                self._audit(name, intent_id, "automatic_reconciliation_pending", {
                    "error_type": type(error).__name__, "order_retry_performed": False,
                }, once=True)
            return
        spec = MONITOR_SPECS.get(name)
        if spec is None or not spec.automation_allowed:
            candidate = asset.get("intent_id")
            if asset.get("can_execute_demo") is True and isinstance(candidate, str):
                self._audit(name, candidate, "operator_approval_required", {
                    "reason": "unattended automation is disabled for this asset class",
                    "order_submitted": False,
                }, once=True)
            return
        if asset.get("kill_switch") is True or asset.get("risk_active") is True:
            return
        if asset.get("can_execute_demo") is not True:
            return
        intent_id = asset.get("intent_id")
        if not isinstance(intent_id, str):
            return
        if asset.get("automation_review_required") is True:
            self._audit(name, intent_id, "operator_approval_required", {
                "reason": asset.get("automation_review_reason"),
                "order_submitted": False,
            }, once=True)
            return
        paused = self.monitors.pause_for_execution(name)
        try:
            self.actions.execute_demo_intent(
                name,
                intent_id,
                "rule-based-demo-automation",
                ARMING_PHRASE,
                str(DASHBOARD_MAX_DEMO_ORDER_USD),
            )
            self._audit(name, intent_id, "routine_demo_order_submitted", {
                "monitor_paused": paused, "order_submitted": True,
            }, once=True)
        except EtoroDemoError as error:
            self.monitors.resume_after_failed_execution_if_safe(name)
            self._audit(name, intent_id, "automatic_execution_stopped", {
                "error_type": type(error).__name__, "automatic_retry": False,
            }, once=True)

    def _audit(
        self,
        asset: str,
        intent_id: str,
        action: str,
        details: Mapping[str, Any],
        *,
        once: bool = False,
    ) -> None:
        record = {
            "schema_version": 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            "action": action,
            "asset": asset.upper(),
            "intent_id": intent_id,
            "environment": "etoro_demo",
            "real_account_allowed": False,
            "leverage": 1,
            **dict(details),
        }
        path = self.data_dir / "demo-automation-audit.jsonl"
        if once:
            _append_jsonl_once(path, f"{intent_id}:{action}", {
                **record, "intent_id": f"{intent_id}:{action}", "source_intent_id": intent_id,
            })
        else:
            _append_jsonl(path, record)


def serve_dashboard(
    data_dir: str | Path,
    port: int,
    project_dir: str | Path = ".",
    authenticator: DashboardAuthenticator | None = None,
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
    source = DashboardData(data_dir)
    actions = DashboardActions(data_dir, project_dir)
    monitors = DashboardMonitorManager(project_dir, data_dir)
    automation = DemoAutomationCoordinator(source, actions, monitors, data_dir)
    action_token = secrets.token_urlsafe(32)
    auth = authenticator or DashboardAuthenticator.from_environment()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = self.path.split("?", 1)[0]
            if path == "/login":
                if self._authenticated():
                    self._redirect("/")
                else:
                    self._send(200, "text/html; charset=utf-8", LOGIN_HTML.encode())
            elif not self._authenticated():
                if path.startswith("/api/"):
                    self._json(401, {"ok": False, "error": "authentication required"})
                else:
                    self._redirect("/login")
            elif path in ("/", "/index.html"):
                page = HTML.replace("__ACTION_TOKEN__", action_token)
                self._send(200, "text/html; charset=utf-8", page.encode())
            elif path == "/api/status":
                document = source.snapshot().as_dict()
                for asset in document["assets"]:
                    name = str(asset["asset"])
                    asset["monitor_running"] = monitors.running(name)
                    asset["monitor_error"] = monitors.last_error(name)
                body = json.dumps(document, separators=(",", ":")).encode()
                self._send(200, "application/json; charset=utf-8", body)
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                path = self.path.split("?", 1)[0]
                if path == "/api/login":
                    self._login()
                    return
                if not self._authenticated():
                    self._json(401, {"ok": False, "error": "authentication required"})
                    return
                if path == "/api/logout":
                    if not self._valid_action_token(action_token):
                        self._json(403, {"ok": False, "error": "invalid action token"})
                        return
                    auth.logout(self._session_token())
                    self._json(200, {"ok": True}, clear_cookie=True)
                    return
                if not self._valid_action_token(action_token):
                    self._json(403, {"ok": False, "error": "invalid action token"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._json(415, {"ok": False, "error": "JSON required"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 4096:
                    self._json(400, {"ok": False, "error": "invalid request size"})
                    return
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, Mapping):
                    raise ValueError("request must be an object")
                parts = self.path.strip("/").split("/")
                if len(parts) != 5 or parts[:2] != ["api", "assets"]:
                    self._json(404, {"ok": False, "error": "Not found"})
                    return
                asset, category, operation = parts[2], parts[3], parts[4]
                actor = value.get("actor")
                if asset == "portfolio" and category == "risk":
                    reason = value.get("reason")
                    if not isinstance(actor, str) or not isinstance(reason, str):
                        raise ValueError("actor and reason must be text")
                    if operation == "kill-enable":
                        result = actions.set_portfolio_kill_switch(
                            True, actor, reason
                        )
                    elif operation == "kill-disable":
                        result = actions.set_portfolio_kill_switch(
                            False, actor, reason
                        )
                    elif operation == "reset-halt":
                        result = actions.reset_portfolio_risk_halt(actor, reason)
                    else:
                        self._json(404, {"ok": False, "error": "Not found"})
                        return
                elif asset == "portfolio" and category == "automation" and operation in ("enable", "disable"):
                    acknowledgement = value.get("acknowledgement", "")
                    if not isinstance(actor, str) or not isinstance(acknowledgement, str):
                        raise ValueError("actor and acknowledgement must be text")
                    result = actions.set_demo_automation(
                        operation == "enable", actor, acknowledgement
                    )
                elif category == "risk" and operation in ("approve", "refuse"):
                    event_id = value.get("event_id")
                    if not isinstance(event_id, str) or not isinstance(actor, str):
                        raise ValueError("event_id and actor must be text")
                    result = (
                        actions.approve(asset, event_id, actor)
                        if operation == "approve"
                        else actions.refuse(asset, event_id, actor)
                    )
                elif category == "kill-switch" and operation == "disable":
                    if not isinstance(actor, str):
                        raise ValueError("actor must be text")
                    result = actions.reenable(asset, actor)
                elif category == "monitor" and operation == "start":
                    result = monitors.start(asset)
                elif category == "monitor" and operation == "stop":
                    result = monitors.stop(asset)
                elif category == "intent" and operation == "execute-demo":
                    intent_id = value.get("intent_id")
                    phrase = value.get("arming_phrase")
                    cap = value.get("max_order_usd")
                    if not all(isinstance(item, str) for item in (intent_id, actor, phrase, cap)):
                        raise ValueError(
                            "intent_id, actor, arming_phrase and max_order_usd must be text"
                        )
                    paused = monitors.pause_for_execution(asset)
                    try:
                        result = dict(actions.execute_demo_intent(
                            asset, intent_id, actor, phrase, cap
                        ))
                        result["monitor_paused_for_execution"] = paused
                    except Exception:
                        monitors.resume_after_failed_execution_if_safe(asset)
                        raise
                elif category == "intent" and operation == "reconcile-demo":
                    intent_id = value.get("intent_id")
                    if not isinstance(intent_id, str):
                        raise ValueError("intent_id must be text")
                    if monitors.running(asset):
                        raise EtoroDemoError(
                            "stop the asset monitor before Demo reconciliation"
                        )
                    result = actions.reconcile_demo_execution(asset, intent_id)
                    try:
                        restarted = monitors.resume_after_reconciliation(asset)
                        result = dict(result)
                        result["monitor_restarted"] = restarted
                    except EtoroDemoError as restart_error:
                        result = dict(result)
                        result["monitor_restarted"] = False
                        result["monitor_restart_warning"] = str(restart_error)
                elif category == "intent" and operation == "resolve-rejected-close":
                    intent_id = value.get("intent_id")
                    acknowledgement = value.get("acknowledgement")
                    if not all(
                        isinstance(item, str)
                        for item in (intent_id, actor, acknowledgement)
                    ):
                        raise ValueError(
                            "intent_id, actor and acknowledgement must be text"
                        )
                    if monitors.running(asset):
                        raise EtoroDemoError(
                            "stop the asset monitor before resolving the rejected close"
                        )
                    result = actions.resolve_rejected_close(
                        asset, intent_id, actor, acknowledgement
                    )
                elif category == "intent" and operation == "dismiss":
                    intent_id = value.get("intent_id")
                    if not isinstance(intent_id, str) or not isinstance(actor, str):
                        raise ValueError("intent_id and actor must be text")
                    result = actions.dismiss_unexecuted_intent(
                        asset, intent_id, actor
                    )
                elif category == "intent" and operation == "abandon-rebaseline":
                    if not isinstance(actor, str):
                        raise ValueError("actor must be text")
                    if monitors.running(asset):
                        raise EtoroDemoError("stop the asset monitor before rebaselining")
                    result = actions.abandon_and_rebaseline(asset, actor)
                elif category == "state" and operation == "rebaseline-flat":
                    if not isinstance(actor, str):
                        raise ValueError("actor must be text")
                    if monitors.running(asset):
                        raise EtoroDemoError("stop the asset monitor before rebaselining")
                    result = actions.confirm_flat_and_rebaseline(asset, actor)
                    monitors.clear_error(asset)
                else:
                    self._json(404, {"ok": False, "error": "Not found"})
                    return
                self._json(200, result)
            except (EtoroDemoError, ValueError, json.JSONDecodeError) as error:
                self._json(409, {"ok": False, "error": str(error)})
            except Exception as error:  # Last-resort safety boundary for request threads.
                # Never let an action handler silently drop the localhost connection:
                # the browser would only report "Failed to fetch", leaving the operator
                # unable to tell whether an order was submitted.  Do not expose the
                # exception text because broker responses or local paths may be present.
                try:
                    _append_jsonl(
                        data_dir / "dashboard-errors.jsonl",
                        {
                            "recorded_at": datetime.now(UTC).isoformat(),
                            "error_type": type(error).__name__,
                            "request_path": self.path,
                            "order_submission_assumed": False,
                        },
                    )
                    self._json(
                        500,
                        {
                            "ok": False,
                            "error": (
                                "dashboard action failed safely before completion "
                                f"({type(error).__name__}); no retry was performed"
                            ),
                        },
                    )
                except Exception:
                    # Even a local audit-disk failure must still produce an HTTP reply.
                    self._json(
                        500,
                        {
                            "ok": False,
                            "error": "dashboard action failed safely; no retry was performed",
                        },
                    )

        def _login(self) -> None:
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"ok": False, "error": "JSON required"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096:
                self._json(400, {"ok": False, "error": "invalid request size"})
                return
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, Mapping):
                raise ValueError("request must be an object")
            username, password = value.get("username"), value.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError("username and password must be text")
            token = auth.login(username, password, self.client_address[0])
            if token is None:
                time.sleep(0.25)
                self._json(401, {"ok": False, "error": "invalid username or password"})
                return
            self._json(200, {"ok": True}, session_token=token)

        def _session_token(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie.get(SESSION_COOKIE)
            return value.value if value is not None else ""

        def _authenticated(self) -> bool:
            return auth.authenticated(self._session_token())

        def _valid_action_token(self, expected: str) -> bool:
            return secrets.compare_digest(
                self.headers.get("X-Dashboard-Token", ""), expected
            )

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _json(
            self,
            status: int,
            value: Mapping[str, Any],
            *,
            session_token: str | None = None,
            clear_cookie: bool = False,
        ) -> None:
            self._send(
                status,
                "application/json; charset=utf-8",
                json.dumps(value, separators=(",", ":")).encode(),
                session_token=session_token,
                clear_cookie=clear_cookie,
            )

        def _send(
            self,
            status: int,
            content_type: str,
            body: bytes,
            *,
            session_token: str | None = None,
            clear_cookie: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            if session_token is not None:
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}={session_token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(SESSION_LIFETIME.total_seconds())}",
                )
            elif clear_cookie:
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
                )
            self.end_headers()
            # Browsers legitimately cancel an in-flight status response when an
            # expired session redirects to /login. Never retry an HTTP action.
            _write_http_body(self.wfile, body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    automation.start()
    try:
        server.serve_forever()
    finally:
        automation.stop()
        monitors.stop_all()
        server.server_close()


def _last_json(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        last = ""
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    last = line
        value = json.loads(last) if last else {}
        if not isinstance(value, Mapping):
            raise ValueError("record must be an object")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        return {"_error": f"invalid {path.name}: {error}"}


def _automation_state(data_dir: str | Path) -> Mapping[str, Any]:
    state = _document(Path(data_dir) / "demo-automation-state.json")
    if not state:
        return {
            "enabled": False,
            "environment": "etoro_demo",
            "routine_actions_automatic": False,
            "unusual_drop_requires_approval": True,
            "unusual_climb_sell_requires_approval": True,
            "real_account_allowed": False,
            "leverage": 1,
        }
    return state


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(dict(value), file, sort_keys=True, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise EtoroDemoError(f"could not write Demo automation state: {error}") from error


def _last_monitor_halt(path: Path) -> str | None:
    """Return the final safety halt emitted by a dashboard-owned monitor."""

    if not path.exists():
        return None
    try:
        message = None
        previous = None
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                clean = line.strip()
                if clean == "eToro Demo continuous execution-readiness monitor":
                    message = None
                    previous = clean
                    continue
                if clean.startswith("HALTED safely"):
                    message = (
                        f"HALTED safely: {previous}"
                        if clean == "HALTED safely; operator review is required."
                        and previous
                        else clean
                    )
                if clean:
                    previous = clean
        return message
    except (OSError, UnicodeError):
        return None


def _last_cli_error(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if "error:" in line.lower():
            return line.split("error:", 1)[-1].strip()
    return "Demo execution command failed; inspect the execution ledger before retrying"


def _document(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("document must be an object")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        return {"_error": f"invalid {path.name}: {error}"}


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as file:
            return sum(1 for line in file if line.strip())
    except (OSError, UnicodeError):
        return 0


def _execution_summary(path: Path) -> Mapping[str, Any]:
    """Summarise ledger state without exposing broker response identifiers."""

    if not path.exists():
        return {"status": "none", "pending": False, "intent_id": None}
    try:
        rows: list[Mapping[str, Any]] = []
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    raise ValueError("ledger row must be an object")
                rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise EtoroDemoError("execution ledger is invalid") from error
    if not rows:
        return {"status": "none", "pending": False, "intent_id": None}
    terminal = {"position_reconciled", "position_closed", "request_rejected"}
    statuses: dict[str, str] = {}
    order: list[str] = []
    for row in rows:
        intent_id = row.get("intent_id")
        status = row.get("status")
        if not isinstance(intent_id, str) or not isinstance(status, str):
            raise EtoroDemoError("execution ledger is invalid")
        if intent_id not in statuses:
            order.append(intent_id)
        statuses[intent_id] = status
    pending_id = next(
        (
            intent_id
            for intent_id in reversed(order)
            if statuses[intent_id] not in terminal
        ),
        None,
    )
    if pending_id is not None:
        return {
            "status": statuses[pending_id],
            "pending": True,
            "intent_id": pending_id,
        }
    latest_id = order[-1]
    return {
        "status": statuses[latest_id],
        "pending": False,
        "intent_id": latest_id,
    }


def _append_jsonl_once(
    path: Path, intent_id: str, record: Mapping[str, Any]
) -> None:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as file:
                for line in file:
                    if line.strip() and json.loads(line).get("intent_id") == intent_id:
                        return
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as error:
            raise EtoroDemoError(f"invalid abandonment audit {path}") from error
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(dict(record), sort_keys=True, separators=(",", ":")))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
    except OSError as error:
        raise EtoroDemoError(f"could not write abandonment audit: {error}") from error


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(dict(record), sort_keys=True, separators=(",", ":")))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
    except OSError as error:
        raise EtoroDemoError(f"could not write portfolio control audit: {error}") from error


def _pick(primary: Mapping[str, Any], secondary: Mapping[str, Any], key: str) -> Any:
    return primary.get(key, secondary.get(key))


def _actor(value: str) -> str:
    actor = value.strip()
    if not actor or len(actor) > 80:
        raise EtoroDemoError("operator name must contain 1 to 80 characters")
    return actor


def _dashboard_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _iso_at_or_after(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        left_at = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_at = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except ValueError:
        return False
    if left_at.tzinfo is None or right_at.tzinfo is None:
        return False
    return left_at.astimezone(UTC) >= right_at.astimezone(UTC)


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Trading Simulator</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--panel:#101d2e;--line:#24364e;--text:#ecf4ff;--muted:#92a7c1;--green:#35d07f;--amber:#f2b84b;--red:#ff6577;--blue:#63a7ff}*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,Segoe UI,sans-serif;background:radial-gradient(circle at top right,#11284a 0,#07111f 38%);color:var(--text);min-height:100vh}.wrap{max-width:1180px;margin:auto;padding:32px 22px}header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:24px}h1{margin:0;font-size:clamp(25px,4vw,42px);letter-spacing:-.04em}.sub{color:var(--muted);margin-top:7px}.safety{border:1px solid #216944;background:#0c2b20;color:#a8f2c7;border-radius:999px;padding:8px 13px;font-weight:700;white-space:nowrap}.portfolio{margin-bottom:18px;background:#0c1929;border:1px solid var(--line);border-radius:17px;padding:17px 19px}.portfolio .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px;margin-top:12px}.metric{background:#101f33;border-radius:10px;padding:10px}.metric small{display:block;color:var(--muted)}.metric strong{font-size:17px}.tabs{display:flex;gap:8px;margin:0 0 18px;padding:5px;background:#0c1929;border:1px solid var(--line);border-radius:12px;width:max-content}.tabs button{border:0;border-radius:8px;padding:9px 18px;background:transparent;color:var(--muted);font-weight:800;cursor:pointer}.tabs button.active{background:var(--blue);color:#061426}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:16px}.card{background:linear-gradient(180deg,#13233a,#0e1929);border:1px solid var(--line);border-radius:17px;padding:19px;box-shadow:0 16px 42px #0005}.top{display:flex;justify-content:space-between;align-items:center}.asset{font-size:23px;font-weight:800}.badge{font-size:12px;border-radius:999px;padding:5px 9px;background:#24334a;color:var(--muted);text-transform:uppercase;font-weight:800}.badge.good{background:#123b2b;color:var(--green)}.badge.warn{background:#493617;color:var(--amber)}.badge.bad{background:#4a1e28;color:var(--red)}dl{display:grid;grid-template-columns:100px 1fr;gap:8px 10px;margin:20px 0 0}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}.reason{margin-top:17px;padding-top:14px;border-top:1px solid var(--line);color:#c7d5e7;min-height:60px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.actions button{border:0;border-radius:9px;padding:8px 11px;font-weight:800;cursor:pointer}.approve{background:var(--green);color:#062014}.refuse{background:var(--red);color:white}.footer{display:flex;justify-content:space-between;gap:15px;margin-top:20px;color:var(--muted);font-size:13px}.empty{opacity:.65}.error{color:var(--red)}@media(max-width:650px){header{display:block}.safety{display:inline-block;margin-top:14px}.footer{display:block}}
</style></head><body><main class="wrap"><header><div><h1>Execution Readiness</h1><div class="sub">Authenticated audit dashboard · BTC / ETH / SOL / XRP / LAC</div><div class="sub">LAC is an exchange-traded equity and requires manual Demo approval; unattended automation is disabled.</div></div><div><div class="safety">● REAL ORDERS BLOCKED · SUPERVISED DEMO · 1×</div><button class="logout" onclick="logout()" style="float:right;margin-top:10px;border:1px solid var(--line);border-radius:8px;padding:7px 11px;background:#13233a;color:var(--text);cursor:pointer">Log out</button></div></header><section id="portfolio" class="portfolio"></section><nav class="tabs" role="tablist" aria-label="Asset class"><button id="crypto-tab" class="active" role="tab" aria-selected="true" onclick="selectAssetClass('crypto')">Crypto</button><button id="equity-tab" role="tab" aria-selected="false" onclick="selectAssetClass('equity')">Stocks</button></nav><section id="grid" class="grid" aria-live="polite"></section><div class="footer"><span id="updated">Waiting for audit data…</span><span>Credentials and raw broker responses are never displayed.</span></div></main>
<script>
const TOKEN='__ACTION_TOKEN__';let activeClass='crypto',latestStatus=null;const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function selectAssetClass(kind){activeClass=kind;document.querySelectorAll('.tabs button').forEach(b=>{const selected=b.id===kind+'-tab';b.classList.toggle('active',selected);b.setAttribute('aria-selected',String(selected))});renderAssets()}
function renderAssets(){if(!latestStatus)return;const assets=latestStatus.assets.filter(a=>a.asset_class===activeClass);document.querySelector('#grid').innerHTML=assets.length?assets.map(card).join(''):'<div class="sub">No assets configured in this class.</div>'}
const pct=v=>v===undefined||v===null?'—':(Number(v)*100).toFixed(2)+'%';
function portfolio(p,a){let automation=a?.enabled?`<span class="badge good">routine Demo automation enabled</span>`:`<span class="badge warn">Demo automation off</span>`;let autoControl=a?.enabled?`<button class="refuse" onclick="automationControl('disable')">Disable Demo automation</button>`:`<button class="approve" onclick="automationControl('enable')">Enable rule-based Demo automation</button>`;if(!p||!p.updated_at)return `<div class="top"><span class="asset">Portfolio risk</span>${automation}</div><div class="sub">Waiting for the first successful Demo portfolio observation.</div><div class="actions">${autoControl}</div>`;let halted=p.manual_kill_switch||p.risk_halt;let controls=p.manual_kill_switch?`<button class="approve" onclick="portfolioControl('kill-disable')">Disable global kill switch</button>`:`<button class="refuse" onclick="portfolioControl('kill-enable')">Enable global kill switch</button>`;if(p.risk_halt)controls+=`<button class="refuse" onclick="portfolioControl('reset-halt')">Reset latched risk halt</button>`;return `<div class="top"><span class="asset">Portfolio risk</span>${automation}</div><div class="metrics"><div class="metric"><small>Cash available</small><strong>${esc(p.available_cash_usd)} USD</strong></div><div class="metric"><small>Invested exposure</small><strong>${esc(p.total_exposure_usd)} USD</strong></div><div class="metric"><small>Reserved intents</small><strong>${esc(p.reserved_exposure_usd)} USD · ${esc(p.reservation_count)}</strong></div><div class="metric"><small>Remaining capacity</small><strong>${esc(p.remaining_total_capacity_usd)} / ${esc(p.maximum_total_exposure_usd)} USD</strong></div><div class="metric"><small>Open assets</small><strong>${esc(p.open_asset_count)} / ${esc(p.maximum_open_assets)}</strong></div><div class="metric"><small>Daily loss</small><strong>${pct(p.daily_loss_rate)}</strong></div><div class="metric"><small>Peak drawdown</small><strong>${pct(p.drawdown_rate)}</strong></div><div class="metric"><small>Cash reserve</small><strong>${esc(p.minimum_cash_reserve_usd)} USD</strong></div><div class="metric"><small>Per-asset cap</small><strong>${esc(p.maximum_asset_exposure_usd)} USD</strong></div></div>${p.halt_reason?`<div class="reason error">${esc(p.halt_reason)}</div>`:''}<div class="actions">${autoControl}${controls}</div>`}
function card(a){let failure=a.error||a.monitor_error;let state=a.reconciliation_pending?'reconcile':failure?'monitor stopped':!a.available?'waiting':a.kill_switch?'kill switch':a.risk_active||a.automation_review_required?'review':a.ready?'intent reserved':a.holding_position?'holding':(a.decision||'observing');let cls=failure||a.kill_switch?'bad':a.reconciliation_pending||a.risk_active||a.automation_review_required?'warn':a.ready||a.holding_position?'good':'';let review=a.risk_active&&!a.kill_switch&&!a.reconciliation_pending?`<button class="approve" onclick="risk('${a.asset}','${a.risk_event_id}','approve')">Approve event</button><button class="refuse" onclick="risk('${a.asset}','${a.risk_event_id}','refuse')">Refuse & halt</button>`:'';let reenable=a.kill_switch?`<button class="approve" onclick="reenable('${a.asset}')">Re-enable</button>`:'';let abandon=a.can_rebaseline&&!a.monitor_running&&!a.reconciliation_pending?`<button class="refuse" onclick="abandonIntent('${a.asset}')">Reject intent & release reservation</button>`:'';let flat=a.can_flat_rebaseline&&!a.monitor_running?`<button class="approve" onclick="flatRebaseline('${a.asset}')">Confirm Demo flat & rebaseline</button>`:'';let close=a.intent_action==='close-entire-long-position';let add=a.intent_action==='add-long-by-cash-amount';let execute=a.can_execute_demo&&!a.kill_switch&&!a.reconciliation_pending?`<button class="refuse" onclick="executeDemo('${a.asset}','${a.intent_id}','${a.intent_amount_usd}','${a.intent_action}')">${a.automation_review_required?'Review unusual climb & DEMO sell':close?'Execute DEMO lot close':add?'Approve DEMO additional buy':'Approve & execute DEMO buy'}</button>`:'';let reconcile=a.reconciliation_pending&&!a.monitor_running?`<button class="approve" onclick="reconcileDemo('${a.asset}','${a.reconciliation_intent_id}')">Reconcile Demo result</button>`:'';let start=!a.kill_switch&&!a.monitor_running&&!a.can_rebaseline&&!a.can_flat_rebaseline&&!a.can_execute_demo&&!a.reconciliation_pending?`<button class="approve" onclick="startMonitor('${a.asset}')">Start monitoring</button>`:'';let running=a.monitor_running?`<button class="refuse" onclick="stopMonitor('${a.asset}')">Stop monitoring</button><span class="badge good">monitor running</span>`:'';let amount=a.intent_amount_usd?`${esc(a.intent_amount_usd)} USD`:'—';let allocation=a.position_allocation_usd?`${esc(a.position_allocation_usd)} USD`:'—';let reason=a.automation_review_required?a.automation_review_reason:a.reconciliation_pending?'Demo execution requires read-only reconciliation. Do not submit or retry the order.':(failure||a.reason||'No completed evaluation recorded yet.');return `<article class="card ${a.available?'':'empty'}"><div class="top"><span class="asset">${esc(a.asset)}</span><span class="badge ${cls}">${esc(state)}</span></div><dl><dt>Resolution</dt><dd>${esc(a.resolution)}</dd><dt>Candle</dt><dd>${esc(a.candle_timestamp)}</dd><dt>State</dt><dd>${esc(a.market_state)}</dd><dt>Decision</dt><dd>${esc(a.decision)}</dd><dt>Intents</dt><dd>${esc(a.intent_count)}</dd><dt>Intent amount</dt><dd>${amount}</dd><dt>Position</dt><dd>${a.holding_position?'open':'flat'}</dd><dt>Lots</dt><dd>${esc(a.position_lots)}</dd><dt>Allocation</dt><dd>${allocation}</dd><dt>Execution</dt><dd>${esc(a.execution_status)}</dd><dt>Leverage</dt><dd>1× · no borrowing</dd></dl><div class="reason ${failure&&!a.reconciliation_pending?'error':''}">${esc(reason)}</div><div class="actions">${review}${reenable}${abandon}${flat}${execute}${reconcile}${start}${running}</div></article>`}
async function risk(asset,event,op){const actor=prompt('Operator name');if(!actor)return;if(op==='refuse'&&!confirm('Refuse this exact event and enable the local kill switch for '+asset+'?'))return;try{const r=await fetch(`/api/assets/${asset.toLowerCase()}/risk/${op}`,{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':TOKEN},body:JSON.stringify({event_id:event,actor})});const d=await r.json();if(!r.ok)throw Error(d.error||'Action failed');alert(op==='approve'?'Event approved. No order was submitted.':'Event refused and kill switch enabled. No order was submitted.');await refresh()}catch(e){alert(e.message)}}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':TOKEN},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error||'Action failed');await refresh();return d}
async function logout(){try{await fetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':TOKEN},body:'{}'})}finally{location.href='/login'}}
async function portfolioControl(op){const actor=prompt('Operator name');if(!actor)return;const reason=prompt('Reason (required and written to the audit log)');if(!reason)return;let message=op==='kill-enable'?'Halt all new portfolio buying? Monitoring and risk-reducing closes remain available.':op==='kill-disable'?'Disable the manual portfolio kill switch? Latched loss controls remain active.':'Reset the latched portfolio loss/drawdown halt and establish a new baseline?';if(!confirm(message))return;try{await post(`/api/assets/portfolio/risk/${op}`,{actor,reason});alert('Portfolio control updated. No order was submitted.')}catch(e){alert(e.message)}}
async function automationControl(op){const actor=prompt('Operator name');if(!actor)return;let ack='';if(op==='enable'){ack=prompt('Routine rule-based actions will submit to eToro DEMO without per-order approval. Structural drops and unusual-climb exits still pause. Real orders and leverage remain blocked. Type exactly:\nI_ENABLE_RULE_BASED_DEMO_AUTOMATION');if(ack!=='I_ENABLE_RULE_BASED_DEMO_AUTOMATION')return;if(!confirm('Enable persistent rule-based automation for the DEMO portfolio?'))return}else if(!confirm('Disable Demo automation? Existing positions and monitors are preserved.'))return;try{await post(`/api/assets/portfolio/automation/${op}`,{actor,acknowledgement:ack});alert(op==='enable'?'Routine Demo automation enabled.':'Demo automation disabled.')}catch(e){alert(e.message)}}
async function reenable(asset){const actor=prompt('Operator name');if(!actor)return;if(!confirm('Disable the local kill switch for '+asset+'? The active risk may still require approval.'))return;try{await post(`/api/assets/${asset.toLowerCase()}/kill-switch/disable`,{actor});alert('Kill switch disabled. No order was submitted.')}catch(e){alert(e.message)}}
async function startMonitor(asset){if(!confirm('Start the read-only readiness monitor for '+asset+'?'))return;try{await post(`/api/assets/${asset.toLowerCase()}/monitor/start`,{});alert(asset+' monitoring started. The monitor cannot submit orders.')}catch(e){alert(e.message)}}
async function stopMonitor(asset){if(!confirm('Stop the '+asset+' readiness monitor? No order will be submitted.'))return;try{await post(`/api/assets/${asset.toLowerCase()}/monitor/stop`,{});alert(asset+' monitoring stopped.')}catch(e){alert(e.message)}}
async function executeDemo(asset,intent,amount,action){const closing=action==='close-entire-long-position';const adding=action==='add-long-by-cash-amount';const actor=prompt('Operator name');if(!actor)return;let cap='1000.00';if(!closing){cap=prompt('Maximum DEMO order amount in USD (hard dashboard limit 1000.00)',amount);if(!cap)return}const phrase=prompt(`This submits a virtual eToro DEMO ${closing?'FULL LOT CLOSE':adding?'ADDITIONAL BUY':'buy'}. Type exactly:\nI_UNDERSTAND_THIS_SUBMITS_A_DEMO_ORDER`);if(!phrase)return;const detail=closing?'close one recorded '+asset+' position lot':`${adding?'add ':''}buy ${amount} USD of ${asset} at 1×`;if(!confirm(`Submit DEMO-only intent ${intent} to ${detail}? This cannot be automatically retried.`))return;try{const d=await post(`/api/assets/${asset.toLowerCase()}/intent/execute-demo`,{actor,intent_id:intent,max_order_usd:cap,arming_phrase:phrase});try{const r=await post(`/api/assets/${asset.toLowerCase()}/intent/reconcile-demo`,{intent_id:intent});alert((r.position_open?asset+' Demo position reconciled and recorded.':asset+' Demo lot close reconciled.')+(r.monitor_restarted?' Monitoring restarted automatically.':' Monitoring can now be restarted.'));await refresh()}catch(re){alert('Demo order response was received and must NOT be submitted again. Automatic reconciliation did not complete: '+re.message+' Use Reconcile Demo result. Intent: '+d.intent_id)}}catch(e){alert(e.message)}}
async function reconcileDemo(asset,intent){if(!confirm(`Read the Demo portfolio and reconcile ${asset} intent ${intent}? This cannot submit or retry an order and may take up to 60 seconds.`))return;try{const d=await post(`/api/assets/${asset.toLowerCase()}/intent/reconcile-demo`,{intent_id:intent});alert(d.position_open?asset+' Demo position reconciled and recorded.':asset+' Demo close reconciled; the position is confirmed closed.');await refresh()}catch(e){alert(e.message)}}
async function abandonIntent(asset){const actor=prompt('Operator name');if(!actor)return;if(!confirm('Reject the latest UNEXECUTED buy intent, release its reserved capacity, verify Demo is flat, and advance the '+asset+' baseline? This does not submit an order.'))return;try{const d=await post(`/api/assets/${asset.toLowerCase()}/intent/abandon-rebaseline`,{actor});alert('Intent rejected, reservation released, and '+asset+' rebaselined at '+d.baseline+'. No order was submitted.')}catch(e){alert(e.message)}}
async function flatRebaseline(asset){const actor=prompt('Operator name');if(!actor)return;if(!confirm('Perform a fresh read-only Demo check, confirm '+asset+' has no position, preserve other assets, and reset only its replay baseline? No order will be submitted.'))return;try{const d=await post(`/api/assets/${asset.toLowerCase()}/state/rebaseline-flat`,{actor});alert(asset+' was confirmed flat and rebaselined at '+d.baseline+'. No order was submitted. You may now restart monitoring.')}catch(e){alert(e.message)}}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(r.status===401){location.href='/login';return}if(!r.ok)throw Error('HTTP '+r.status);const d=await r.json();latestStatus=d;document.querySelector('#portfolio').innerHTML=portfolio(d.portfolio_risk,d.automation);renderAssets();document.querySelector('#updated').textContent='Updated '+new Date(d.generated_at).toLocaleString()}catch(e){document.querySelector('#updated').textContent='Dashboard refresh failed: '+e.message}}
async function resolveRejectedClose(asset,intent){const actor=prompt('Operator name');if(!actor)return;const phrase=prompt('The previous request displayed HTTP 400 and will NOT be retried. Type exactly:\nI_SAW_HTTP_400_REJECTION');if(phrase!=='I_SAW_HTTP_400_REJECTION')return;if(!confirm('Perform a fresh read-only check that the exact Demo position remains open with no pending order, then mark only this failed attempt rejected?'))return;try{await post(`/api/assets/${asset.toLowerCase()}/intent/resolve-rejected-close`,{actor,intent_id:intent,acknowledgement:phrase});alert('HTTP 400 attempt marked rejected after confirming the Demo position remains open. No order was submitted or retried.')}catch(e){alert(e.message)}}
async function dismissIntent(asset,intent){const actor=prompt('Operator name');if(!actor)return;if(!confirm(`Dismiss unexecuted ${asset} trigger ${intent}? Monitoring will continue and no order will be submitted.`))return;try{await post(`/api/assets/${asset.toLowerCase()}/intent/dismiss`,{actor,intent_id:intent});alert('Trigger dismissed. Monitoring continues and no order was submitted.');await refresh()}catch(e){alert(e.message)}}
async function addRecoveryButtons(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)return;const d=await r.json();d.assets.forEach((a,i)=>{if(a.can_resolve_rejected_close&&!a.monitor_running){const actions=document.querySelectorAll('.card')[i]?.querySelector('.actions');if(actions&&!actions.querySelector('.resolve-http-400'))actions.insertAdjacentHTML('afterbegin',`<button class="approve resolve-http-400" onclick="resolveRejectedClose('${a.asset}','${a.reconciliation_intent_id}')">Resolve HTTP 400 rejection</button>`)}})}catch(e){}}
async function addDismissButtons(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)return;const d=await r.json();d.assets.forEach((a,i)=>{if(a.can_dismiss_intent&&!a.reconciliation_pending){const actions=document.querySelectorAll('.card')[i]?.querySelector('.actions');if(actions&&!actions.querySelector('.dismiss-trigger'))actions.insertAdjacentHTML('afterbegin',`<button class="approve dismiss-trigger" onclick="dismissIntent('${a.asset}','${a.intent_id}')">Dismiss trigger · keep monitoring</button>`)}})}catch(e){}}
async function enhance(){await addRecoveryButtons();await addDismissButtons()}
refresh().then(enhance);setInterval(()=>refresh().then(enhance),10000);
</script></body></html>'''


LOGIN_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · Codex Trading Simulator</title><style>
:root{color-scheme:dark;--bg:#07111f;--panel:#101d2e;--line:#24364e;--text:#ecf4ff;--muted:#92a7c1;--green:#35d07f;--red:#ff6577}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;font:15px/1.5 system-ui,Segoe UI,sans-serif;background:radial-gradient(circle at top right,#11284a 0,#07111f 42%);color:var(--text)}main{width:min(420px,100%);background:linear-gradient(180deg,#13233a,#0e1929);border:1px solid var(--line);border-radius:18px;padding:30px;box-shadow:0 20px 60px #0008}h1{margin:0;font-size:30px}.sub{color:var(--muted);margin:7px 0 24px}label{display:block;margin:14px 0 6px;color:#c7d5e7}input{width:100%;border:1px solid var(--line);border-radius:9px;background:#081524;color:var(--text);padding:11px 12px;font:inherit}button{width:100%;border:0;border-radius:9px;background:var(--green);color:#062014;padding:11px;margin-top:20px;font-weight:800;cursor:pointer}.error{color:var(--red);min-height:23px;margin-top:12px}.safety{color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:17px;margin-top:17px}
</style></head><body><main><h1>Execution Readiness</h1><div class="sub">Sign in to the supervised Demo dashboard</div><form id="login"><label for="username">Username</label><input id="username" name="username" autocomplete="username" required autofocus><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required><button type="submit">Sign in</button><div id="error" class="error" role="alert"></div></form><div class="safety">Real orders blocked · Demo only · 1× · no borrowing</div></main><script>
document.querySelector('#login').addEventListener('submit',async e=>{e.preventDefault();const button=e.target.querySelector('button');const error=document.querySelector('#error');button.disabled=true;error.textContent='';try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:e.target.username.value,password:e.target.password.value})});const d=await r.json();if(!r.ok)throw Error(d.error||'Sign in failed');location.href='/'}catch(ex){error.textContent=ex.message;e.target.password.value='';button.disabled=false}})
</script></body></html>'''

