from collections.abc import Mapping
from decimal import Decimal

import pytest

from trading_simulator import (
    EtoroCredentials,
    EtoroDemoError,
    EtoroDemoReadOnlyClient,
)
from trading_simulator.etoro_demo import _safe_error_detail


def test_demo_portfolio_uses_only_get_transport_and_expected_headers() -> None:
    calls: list[tuple[str, Mapping[str, str], float]] = []

    def transport(url: str, headers: Mapping[str, str], timeout: float):  # type: ignore[no-untyped-def]
        calls.append((url, headers, timeout))
        return {"data": {"portfolio": "virtual"}}

    client = EtoroDemoReadOnlyClient(
        EtoroCredentials("public-secret", "private-secret"),
        transport=transport,
    )

    assert client.demo_portfolio()["data"]["portfolio"] == "virtual"
    url, headers, timeout = calls[0]
    assert url == "https://public-api.etoro.com/api/v1/trading/info/demo/portfolio"
    assert headers["x-api-key"] == "public-secret"
    assert headers["x-user-key"] == "private-secret"
    assert headers["x-request-id"]
    assert timeout == 10.0


def test_market_data_encodes_parameters() -> None:
    urls: list[str] = []

    def transport(url: str, headers: Mapping[str, str], timeout: float):  # type: ignore[no-untyped-def]
        urls.append(url)
        return {"data": []}

    client = EtoroDemoReadOnlyClient(
        EtoroCredentials("public", "private"), transport=transport
    )

    client.market_data("search", {"search": "BTC USD"})

    assert urls == [
        "https://public-api.etoro.com/api/v1/market-data/search?search=BTC+USD"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://public-api.etoro.com/api/v2/trading/execution/demo/orders",
        "https://public-api.etoro.com/api/v1/trading/info/portfolio",
        "https://public-api.etoro.com/api/v1/trading/info/real/pnl",
        "http://public-api.etoro.com/api/v1/trading/info/demo/portfolio",
        "https://example.com/api/v1/trading/info/demo/portfolio",
    ],
)
def test_allowlist_blocks_execution_real_and_other_origins(url: str) -> None:
    with pytest.raises(EtoroDemoError):
        EtoroDemoReadOnlyClient._validate_url(url)


def test_credentials_are_loaded_from_environment_and_redacted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ETORO_PUBLIC_KEY", "public-value")
    monkeypatch.setenv("ETORO_PRIVATE_KEY", "private-value")

    credentials = EtoroCredentials.from_environment()

    assert credentials.public_key == "public-value"
    assert credentials.private_key == "private-value"
    assert "public-value" not in repr(credentials)
    assert "private-value" not in repr(credentials)


def test_transport_error_does_not_reveal_credentials() -> None:
    def transport(url: str, headers: Mapping[str, str], timeout: float):  # type: ignore[no-untyped-def]
        raise RuntimeError(f"bad request using {headers['x-api-key']}")

    client = EtoroDemoReadOnlyClient(
        EtoroCredentials("do-not-leak-public", "do-not-leak-private"),
        transport=transport,
    )

    with pytest.raises(EtoroDemoError) as captured:
        client.demo_portfolio()
    assert "do-not-leak" not in str(captured.value)


def test_missing_environment_credentials_are_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ETORO_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("ETORO_PRIVATE_KEY", raising=False)

    with pytest.raises(EtoroDemoError, match="ETORO_PUBLIC_KEY"):
        EtoroCredentials.from_environment()


def test_http_error_detail_is_limited_and_redacts_credentials() -> None:
    body = '{"errorCode":"FORBIDDEN","message":"key private-value denied"}'

    detail = _safe_error_detail(
        body, secrets=("public-value", "private-value")
    )

    assert detail == "errorCode=FORBIDDEN; message=key <redacted> denied"


def test_demo_summary_uses_documented_cash_equity_and_pnl_formula() -> None:
    payload = {
        "clientPortfolio": {
            "credit": 1000,
            "positions": [
                {"amount": 200, "unrealizedPnL": {"pnL": 10}}
            ],
            "ordersForOpen": [
                {"mirrorID": 0, "amount": 50, "totalExternalCosts": 2},
                {"mirrorID": 7, "amount": 70, "totalExternalCosts": 1},
            ],
            "orders": [{"amount": 30}],
            "mirrors": [
                {
                    "availableAmount": 20,
                    "closedPositionsNetProfit": 3,
                    "positions": [
                        {"amount": 100, "unrealizedPnL": {"pnL": -5}}
                    ],
                }
            ],
        }
    }

    client = EtoroDemoReadOnlyClient(
        EtoroCredentials("public", "private"),
        transport=lambda url, headers, timeout: payload,
    )
    summary = client.demo_summary()

    assert summary.currency == "USD"
    assert summary.credit == Decimal("1000")
    assert summary.available_cash == Decimal("920")
    assert summary.total_invested == Decimal("399")
    assert summary.unrealized_profit_loss == Decimal("8")
    assert summary.equity == Decimal("1327")
    assert summary.open_position_count == 2
    assert summary.pending_order_count == 2


def test_demo_summary_rejects_missing_portfolio_envelope() -> None:
    client = EtoroDemoReadOnlyClient(
        EtoroCredentials("public", "private"),
        transport=lambda url, headers, timeout: {},
    )

    with pytest.raises(EtoroDemoError, match="missing clientPortfolio"):
        client.demo_summary()
