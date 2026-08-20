import shutil
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from trading_simulator import (
    AuditedIntent,
    EtoroCredentials,
    EtoroDemoError,
    EtoroDemoExecutionClient,
    ExecutionLedger,
    IntentAuditReader,
)


PROJECT = Path(__file__).resolve().parents[1]


def _intent(*, leverage: int = 1) -> AuditedIntent:
    return AuditedIntent(
        intent_id="a" * 64,
        strategy_version="BTC-v1.0",
        candle_timestamp=datetime(2026, 8, 20, 10, tzinfo=UTC),
        action="open-long-by-cash-amount",
        request_path_template="/api/v2/trading/execution/demo/orders",
        request_body={
            "action": "open",
            "transaction": "buy",
            "instrumentId": 100,
            "orderType": "mkt",
            "amount": "1000",
            "orderCurrency": "usd",
            "leverage": leverage,
        },
    )


def test_execution_client_uses_only_exact_demo_url_and_cash_long_body() -> None:
    calls = []

    def transport(url, headers, payload, timeout):  # type: ignore[no-untyped-def]
        calls.append((url, headers, payload, timeout))
        return {"orderId": 123}

    client = EtoroDemoExecutionClient(
        EtoroCredentials("public", "private"), transport=transport
    )
    response = client.submit_open_long(_intent())

    assert response == {"orderId": 123}
    url, headers, payload, timeout = calls[0]
    assert url == "https://public-api.etoro.com/api/v2/trading/execution/demo/orders"
    assert b'"transaction":"buy"' in payload
    assert b'"leverage":1' in payload
    assert headers["x-request-id"]
    assert timeout == 10.0


@pytest.mark.parametrize(
    "url",
    [
        "https://public-api.etoro.com/api/v2/trading/execution/orders",
        "https://public-api.etoro.com/api/v1/trading/execution/demo/orders",
        "https://example.com/api/v2/trading/execution/demo/orders",
    ],
)
def test_execution_client_rejects_real_wrong_version_and_wrong_host(url: str) -> None:
    with pytest.raises(EtoroDemoError):
        EtoroDemoExecutionClient._validate_url(url)


def test_execution_client_rejects_any_leverage_above_one() -> None:
    client = EtoroDemoExecutionClient(
        EtoroCredentials("public", "private"),
        transport=lambda url, headers, payload, timeout: {},
    )
    with pytest.raises(EtoroDemoError, match="cash-only"):
        client.submit_open_long(_intent(leverage=2))


def test_execution_client_closes_only_exact_demo_position_in_full() -> None:
    calls = []
    intent = AuditedIntent(
        intent_id="c" * 64,
        strategy_version="XRP-v1.1-15m",
        candle_timestamp=datetime(2026, 8, 20, 10, tzinfo=UTC),
        action="close-entire-long-position",
        request_path_template=(
            "/api/v1/trading/execution/demo/market-close-orders/positions/123"
        ),
        request_body={"InstrumentId": 100003, "UnitsToDeduct": None},
    )
    client = EtoroDemoExecutionClient(
        EtoroCredentials("public", "private"),
        transport=lambda *args: calls.append(args) or {"orderId": 456},
    )

    assert client.submit_close_long(intent) == {"orderId": 456}
    assert calls[0][0].endswith("/demo/market-close-orders/positions/123")
    assert calls[0][2] == b'{"InstrumentId":100003,"UnitsToDeduct":null}'


def test_write_ahead_ledger_blocks_every_retry_after_attempt() -> None:
    directory = PROJECT / "outputs" / f".pytest-execution-{uuid4()}"
    ledger = ExecutionLedger(directory / "ledger.jsonl")
    try:
        ledger.record_attempt(_intent())
        with pytest.raises(EtoroDemoError, match="retry is blocked"):
            ledger.assert_not_attempted(_intent().intent_id)
        ledger.record_response(_intent().intent_id, {"orderId": 123, "secret": "omit"})
        text = ledger.path.read_text(encoding="utf-8")
        assert '"status":"attempting"' in text
        assert '"orderId":123' in text
        assert "secret" not in text
    finally:
        if directory.exists():
            shutil.rmtree(directory)


def test_definitively_rejected_request_is_terminal_and_not_reconcilable() -> None:
    directory = PROJECT / "outputs" / f".pytest-rejected-{uuid4()}"
    ledger = ExecutionLedger(directory / "ledger.jsonl")
    try:
        ledger.record_attempt(_intent())
        ledger.record_rejection(
            _intent().intent_id,
            reason="HTTP 400 validation rejection",
            confirmed_by="execution-client",
        )
        with pytest.raises(EtoroDemoError, match="rejected"):
            ledger.assert_submitted(_intent().intent_id)
        assert '"order_submitted":false' in ledger.path.read_text(encoding="utf-8")
    finally:
        if directory.exists():
            shutil.rmtree(directory)


def test_execution_reader_rejects_synthetic_intent_audit() -> None:
    directory = PROJECT / "outputs" / f".pytest-synthetic-{uuid4()}"
    path = directory / "intents.jsonl"
    directory.mkdir(parents=True)
    try:
        path.write_text(
            json.dumps(
                {
                    "intent_id": "s" * 64,
                    "environment": "synthetic_test",
                    "execution_eligible": False,
                    "order_submitted": False,
                    "strategy_version": "XRP-v1.0",
                    "candle_timestamp": "2026-08-20T10:00:00+00:00",
                    "action": "open-long-by-cash-amount",
                    "request_path_template": "/api/v2/trading/execution/demo/orders",
                    "request_body": {"leverage": 1},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(EtoroDemoError, match="not an unsubmitted eToro Demo"):
            IntentAuditReader(path).load("s" * 64)
    finally:
        shutil.rmtree(directory)
