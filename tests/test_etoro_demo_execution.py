import shutil
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
