"""Explicitly armed, Demo-only execution for pre-audited long-buy intents."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener
from uuid import NAMESPACE_URL, uuid5

from .etoro_demo import (
    EtoroCredentials,
    EtoroDemoError,
    _RejectRedirects,
    _safe_error_detail,
)


ARMING_PHRASE = "I_UNDERSTAND_THIS_SUBMITS_A_DEMO_ORDER"
DEMO_ORDER_PATH = "/api/v2/trading/execution/demo/orders"
DEMO_CLOSE_PATH_PREFIX = "/api/v1/trading/execution/demo/market-close-orders/positions/"
JsonObject = Mapping[str, Any]
WriteTransport = Callable[[str, Mapping[str, str], bytes, float], JsonObject]


@dataclass(frozen=True, slots=True)
class AuditedIntent:
    intent_id: str
    strategy_version: str
    candle_timestamp: datetime
    action: str
    request_path_template: str
    request_body: Mapping[str, Any]


class IntentAuditReader:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self, intent_id: str) -> AuditedIntent:
        matches: list[Mapping[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as file:
                for line in file:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    if not isinstance(raw, Mapping):
                        raise ValueError("intent record must be an object")
                    if raw.get("intent_id") == intent_id:
                        matches.append(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise EtoroDemoError(f"could not read intent audit {self.path}") from error
        if len(matches) != 1:
            raise EtoroDemoError(
                f"expected exactly one audited intent {intent_id!r}; found {len(matches)}"
            )
        raw = matches[0]
        if raw.get("environment") != "etoro_demo" or raw.get("order_submitted") is not False:
            raise EtoroDemoError("intent is not an unsubmitted eToro Demo intent")
        body = raw.get("request_body")
        if not isinstance(body, Mapping):
            raise EtoroDemoError("audited intent request body is invalid")
        return AuditedIntent(
            intent_id=str(raw["intent_id"]),
            strategy_version=str(raw["strategy_version"]),
            candle_timestamp=_timestamp(raw.get("candle_timestamp")),
            action=str(raw["action"]),
            request_path_template=str(raw["request_path_template"]),
            request_body=dict(body),
        )


class EtoroDemoExecutionClient:
    ORIGIN = "https://public-api.etoro.com"

    def __init__(
        self,
        credentials: EtoroCredentials,
        *,
        transport: WriteTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._credentials = credentials
        self._transport = transport or _post_json
        self._timeout = timeout_seconds

    def submit_open_long(self, intent: AuditedIntent) -> JsonObject:
        _validate_open_long_intent(intent)
        url = f"{self.ORIGIN}{DEMO_ORDER_PATH}"
        self._validate_url(url)
        headers = {
            "x-api-key": self._credentials.public_key,
            "x-user-key": self._credentials.private_key,
            "x-request-id": str(uuid5(NAMESPACE_URL, intent.intent_id)),
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "trading-strategy-simulator/0.27.0 (armed demo only)",
        }
        payload = json.dumps(dict(intent.request_body), separators=(",", ":")).encode(
            "utf-8"
        )
        try:
            return self._transport(url, headers, payload, self._timeout)
        except EtoroDemoError:
            raise
        except Exception as error:
            raise EtoroDemoError(
                f"eToro Demo submission outcome is uncertain ({type(error).__name__}); "
                "do not retry before portfolio reconciliation"
            ) from error

    def submit_close_long(self, intent: AuditedIntent) -> JsonObject:
        position_id = _validate_close_long_intent(intent)
        url = f"{self.ORIGIN}{DEMO_CLOSE_PATH_PREFIX}{position_id}"
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "public-api.etoro.com"
            or parsed.path != f"{DEMO_CLOSE_PATH_PREFIX}{position_id}"
            or parsed.query
        ):
            raise EtoroDemoError("execution client permits only the exact Demo close URL")
        headers = {
            "x-api-key": self._credentials.public_key,
            "x-user-key": self._credentials.private_key,
            "x-request-id": str(uuid5(NAMESPACE_URL, intent.intent_id)),
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "trading-strategy-simulator/0.27.0 (armed demo only)",
        }
        payload = json.dumps(dict(intent.request_body), separators=(",", ":")).encode()
        try:
            return self._transport(url, headers, payload, self._timeout)
        except EtoroDemoError:
            raise
        except Exception as error:
            raise EtoroDemoError(
                f"eToro Demo close outcome is uncertain ({type(error).__name__}); "
                "do not retry before portfolio reconciliation"
            ) from error

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "public-api.etoro.com"
            or parsed.path != DEMO_ORDER_PATH
            or parsed.query
        ):
            raise EtoroDemoError("execution client permits only the exact Demo order URL")


class ExecutionLedger:
    """Write-ahead JSONL ledger; any prior attempt permanently blocks retry."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def assert_not_attempted(self, intent_id: str) -> None:
        if any(row.get("intent_id") == intent_id for row in self._rows()):
            raise EtoroDemoError(
                "intent already has an execution-ledger entry; automatic retry is blocked"
            )

    def record_attempt(self, intent: AuditedIntent) -> None:
        self.assert_not_attempted(intent.intent_id)
        self._append(
            {
                "schema_version": 1,
                "intent_id": intent.intent_id,
                "recorded_at": datetime.now(UTC).isoformat(),
                "environment": "etoro_demo",
                "status": "attempting",
                "order_submitted": "unknown_until_response",
                "leverage": 1,
                "real_account_allowed": False,
            }
        )

    def record_response(self, intent_id: str, response: JsonObject) -> None:
        # Store only response field names and non-sensitive public identifiers.
        safe = {
            key: value
            for key, value in response.items()
            if key in {"token", "orderId", "referenceId", "status"}
            and isinstance(value, (str, int))
        }
        self._append(
            {
                "schema_version": 1,
                "intent_id": intent_id,
                "recorded_at": datetime.now(UTC).isoformat(),
                "environment": "etoro_demo",
                "status": "response_received",
                "order_submitted": True,
                "response": safe,
                "leverage": 1,
                "real_account_allowed": False,
            }
        )

    def record_rejection(
        self, intent_id: str, *, reason: str, confirmed_by: str
    ) -> None:
        if any(
            row.get("intent_id") == intent_id
            and row.get("status") == "request_rejected"
            for row in self._rows()
        ):
            return
        self._append(
            {
                "schema_version": 1,
                "intent_id": intent_id,
                "recorded_at": datetime.now(UTC).isoformat(),
                "environment": "etoro_demo",
                "status": "request_rejected",
                "order_submitted": False,
                "reason": reason,
                "confirmed_by": confirmed_by,
                "leverage": 1,
                "real_account_allowed": False,
            }
        )

    def assert_submitted(self, intent_id: str) -> None:
        rows = [row for row in self._rows() if row.get("intent_id") == intent_id]
        if not rows:
            raise EtoroDemoError("intent has no execution-ledger attempt")
        if rows[-1].get("status") == "request_rejected":
            raise EtoroDemoError("intent request was rejected and cannot be reconciled")
        if not any(
            row.get("status") in {"attempting", "response_received"}
            for row in rows
        ):
            raise EtoroDemoError("intent ledger has no submission attempt")

    def has_attempt(self, intent_id: str) -> bool:
        return any(
            row.get("intent_id") == intent_id
            and row.get("status") in {"attempting", "response_received", "position_reconciled"}
            for row in self._rows()
        )

    def record_reconciliation(
        self, intent_id: str, position: Mapping[str, Any]
    ) -> None:
        if any(
            row.get("intent_id") == intent_id
            and row.get("status") == "position_reconciled"
            for row in self._rows()
        ):
            return
        self._append(
            {
                "schema_version": 1,
                "intent_id": intent_id,
                "recorded_at": datetime.now(UTC).isoformat(),
                "environment": "etoro_demo",
                "status": "position_reconciled",
                "position": dict(position),
                "order_submitted": True,
                "leverage": 1,
                "real_account_allowed": False,
            }
        )

    def record_close_reconciliation(
        self, intent_id: str, position: Mapping[str, Any]
    ) -> None:
        if any(
            row.get("intent_id") == intent_id
            and row.get("status") == "position_closed"
            for row in self._rows()
        ):
            return
        self._append(
            {
                "schema_version": 1,
                "intent_id": intent_id,
                "recorded_at": datetime.now(UTC).isoformat(),
                "environment": "etoro_demo",
                "status": "position_closed",
                "order_submitted": True,
                "position": dict(position),
                "leverage": 1,
                "real_account_allowed": False,
            }
        )

    def _append(self, row: Mapping[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(dict(row), sort_keys=True, separators=(",", ":")))
                file.write("\n")
                file.flush()
        except OSError as error:
            raise EtoroDemoError(f"could not write execution ledger: {error}") from error

    def _rows(self) -> list[Mapping[str, Any]]:
        if not self.path.exists():
            return []
        try:
            rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EtoroDemoError("execution ledger is invalid") from error
        if any(not isinstance(row, Mapping) for row in rows):
            raise EtoroDemoError("execution ledger contains a non-object record")
        return rows


def _validate_open_long_intent(intent: AuditedIntent) -> None:
    body = intent.request_body
    required = {
        "action",
        "transaction",
        "instrumentId",
        "orderType",
        "amount",
        "orderCurrency",
        "leverage",
    }
    if set(body) != required:
        raise EtoroDemoError("Demo intent contains unexpected or missing request fields")
    if intent.request_path_template != DEMO_ORDER_PATH:
        raise EtoroDemoError("intent does not target the exact Demo order endpoint")
    if intent.action not in {"open-long-by-cash-amount", "add-long-by-cash-amount"}:
        raise EtoroDemoError("only audited open-long intents are executable")
    if (
        body["action"] != "open"
        or body["transaction"] != "buy"
        or body["orderType"] != "mkt"
        or body["orderCurrency"] != "usd"
        or body["leverage"] != 1
    ):
        raise EtoroDemoError("intent violates cash-only long-buy invariants")
    if isinstance(body["instrumentId"], bool) or int(body["instrumentId"]) <= 0:
        raise EtoroDemoError("intent instrumentId is invalid")
    try:
        amount = Decimal(str(body["amount"]))
    except (InvalidOperation, ValueError) as error:
        raise EtoroDemoError("intent amount is invalid") from error
    if not amount.is_finite() or amount <= 0:
        raise EtoroDemoError("intent amount is invalid")


def _validate_close_long_intent(intent: AuditedIntent) -> int:
    if intent.action != "close-entire-long-position":
        raise EtoroDemoError("only audited full-position close intents are executable")
    if set(intent.request_body) != {"InstrumentId", "UnitsToDeduct"}:
        raise EtoroDemoError("Demo close intent must close the entire position")
    if intent.request_body["UnitsToDeduct"] is not None:
        raise EtoroDemoError("Demo close intent must close the entire position")
    try:
        instrument_id = int(intent.request_body["InstrumentId"])
    except (TypeError, ValueError) as error:
        raise EtoroDemoError("Demo close intent instrument ID is invalid") from error
    if instrument_id <= 0 or isinstance(intent.request_body["InstrumentId"], bool):
        raise EtoroDemoError("Demo close intent instrument ID is invalid")
    if not intent.request_path_template.startswith(DEMO_CLOSE_PATH_PREFIX):
        raise EtoroDemoError("close intent does not target the Demo close endpoint")
    raw = intent.request_path_template[len(DEMO_CLOSE_PATH_PREFIX):]
    if not raw.isdigit() or int(raw) <= 0 or "/" in raw:
        raise EtoroDemoError("close intent position ID is invalid")
    return int(raw)


def _post_json(
    url: str, headers: Mapping[str, str], payload: bytes, timeout: float
) -> JsonObject:
    request = Request(url, data=payload, headers=dict(headers), method="POST")
    try:
        with build_opener(_RejectRedirects()).open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as error:
        detail = _safe_error_detail(
            error.read(16_384).decode("utf-8", errors="replace"),
            secrets=(headers.get("x-api-key", ""), headers.get("x-user-key", "")),
        )
        raise EtoroDemoError(
            f"eToro Demo submission returned HTTP {error.code}"
            + ("" if detail is None else f": {detail}")
        ) from error
    except URLError as error:
        raise EtoroDemoError(
            "eToro Demo submission outcome is uncertain; do not retry before reconciliation"
        ) from error
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EtoroDemoError("eToro Demo returned invalid JSON after submission") from error
    if not isinstance(result, Mapping):
        raise EtoroDemoError("eToro Demo submission response must be an object")
    return result


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise EtoroDemoError("intent candle timestamp is invalid")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise EtoroDemoError("intent candle timestamp has no timezone")
    return result.astimezone(UTC)
