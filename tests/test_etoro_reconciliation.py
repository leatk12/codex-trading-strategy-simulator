from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_simulator import AuditedIntent, EtoroDemoError
from trading_simulator.etoro_reconciliation import (
    reconcile_closed_long,
    reconcile_open_long,
)


def _intent() -> AuditedIntent:
    return AuditedIntent(
        intent_id="a" * 64,
        strategy_version="XRP-v1.0",
        candle_timestamp=datetime(2026, 8, 20, 10, tzinfo=UTC),
        action="open-long-by-cash-amount",
        request_path_template="/api/v2/trading/execution/demo/orders",
        request_body={"instrumentId": 77, "amount": "1000.00", "leverage": 1},
    )


def _payload(position=None, pending=None):  # type: ignore[no-untyped-def]
    return {"clientPortfolio": {
        "positions": [] if position is None else [position],
        "orders": [] if pending is None else [pending],
        "ordersForOpen": [],
        "mirrors": [],
    }}


def test_reconciliation_waits_while_no_position_exists() -> None:
    assert reconcile_open_long(_intent(), _payload(pending={"amount": 1000})) is None


def test_reconciliation_records_exact_unleveraged_long() -> None:
    result = reconcile_open_long(_intent(), _payload({
        "positionID": 123,
        "instrumentID": 77,
        "isBuy": True,
        "leverage": 1,
        "amount": "1000.00",
        "openRate": "2.50",
        "units": "400",
    }))
    assert result is not None
    assert result.position_id == 123
    assert str(result.units) == "400"


def test_reconciliation_accepts_one_cent_execution_rounding_shortfall() -> None:
    result = reconcile_open_long(_intent(), _payload({
        "positionID": 123,
        "instrumentID": 77,
        "isBuy": True,
        "leverage": 1,
        "amount": "999.99",
        "openRate": "2.50",
        "units": "399.996",
    }))

    assert result is not None
    assert result.amount_usd == Decimal("999.99")


def test_reconciliation_accounts_for_separately_reported_external_costs() -> None:
    result = reconcile_open_long(_intent(), _payload({
        "positionID": 123,
        "instrumentID": 77,
        "isBuy": True,
        "leverage": 1,
        "amount": "997.50",
        "totalExternalCosts": "2.50",
        "openRate": "2.50",
        "units": "399",
    }))

    assert result is not None
    assert result.amount_usd == Decimal("997.50")
    assert result.fees_usd == Decimal("2.50")


def test_reconciliation_accepts_five_cent_btc_style_precision_shortfall() -> None:
    result = reconcile_open_long(_intent(), _payload({
        "positionID": 123,
        "instrumentID": 77,
        "isBuy": True,
        "leverage": 1,
        "amount": "999.95",
        "openRate": "2.50",
    }))
    assert result is not None
    assert result.amount_usd == Decimal("999.95")


@pytest.mark.parametrize("amount", ["999.89", "1000.01"])
def test_reconciliation_rejects_larger_shortfall_or_overfill(amount: str) -> None:
    with pytest.raises(EtoroDemoError, match="partial or altered"):
        reconcile_open_long(_intent(), _payload({
            "positionID": 123,
            "instrumentID": 77,
            "isBuy": True,
            "leverage": 1,
            "amount": amount,
            "openRate": "2.50",
        }))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("isBuy", False, "short position"),
        ("leverage", 2, "leverage"),
        ("amount", "500.00", "partial or altered"),
    ],
)
def test_reconciliation_halts_on_unsafe_difference(field, value, message) -> None:  # type: ignore[no-untyped-def]
    position = {
        "positionID": 123, "instrumentID": 77, "isBuy": True,
        "leverage": 1, "amount": "1000.00", "openRate": "2.50",
    }
    position[field] = value
    with pytest.raises(EtoroDemoError, match=message):
        reconcile_open_long(_intent(), _payload(position))


def test_open_reconciliation_ignores_another_long_unleveraged_asset() -> None:
    payload = _payload({
        "positionID": 555,
        "instrumentID": 88,
        "isBuy": True,
        "leverage": 1,
        "amount": "500.00",
        "openRate": "1.00",
    })
    assert reconcile_open_long(_intent(), payload) is None


def test_additional_buy_reconciliation_identifies_only_the_new_lot() -> None:
    payload = {"clientPortfolio": {
        "positions": [
            {"positionID": 123, "instrumentID": 77, "isBuy": True,
             "leverage": 1, "amount": "1000", "openRate": "2.5"},
            {"positionID": 124, "instrumentID": 77, "isBuy": True,
             "leverage": 1, "amount": "999.99", "openRate": "2.4"},
        ],
        "orders": [], "ordersForOpen": [], "mirrors": [],
    }}
    result = reconcile_open_long(
        _intent(), payload, existing_position_ids=(123,)
    )
    assert result is not None
    assert result.position_id == 124


def test_close_reconciliation_allows_other_assets_after_target_disappears() -> None:
    intent = AuditedIntent(
        intent_id="c" * 64,
        strategy_version="XRP-v1.1-15m",
        candle_timestamp=datetime(2026, 8, 20, 10, tzinfo=UTC),
        action="close-entire-long-position",
        request_path_template=(
            "/api/v1/trading/execution/demo/market-close-orders/positions/123"
        ),
        request_body={"UnitsToDeduct": None},
    )
    payload = _payload({
        "positionID": 456, "instrumentID": 88, "isBuy": True,
        "leverage": 1, "amount": "500", "openRate": "1",
    })

    result = reconcile_closed_long(intent, payload, expected_instrument_id=77)
    assert result is not None
    assert result.position_id == 123


def test_close_reconciliation_rejects_another_position_for_same_asset() -> None:
    intent = AuditedIntent(
        intent_id="c" * 64,
        strategy_version="XRP-v1.1-15m",
        candle_timestamp=datetime(2026, 8, 20, 10, tzinfo=UTC),
        action="close-entire-long-position",
        request_path_template=(
            "/api/v1/trading/execution/demo/market-close-orders/positions/123"
        ),
        request_body={"UnitsToDeduct": None},
    )
    payload = _payload({
        "positionID": 124, "instrumentID": 77, "isBuy": True,
        "leverage": 1, "amount": "500", "openRate": "1",
    })
    with pytest.raises(EtoroDemoError, match="ambiguous"):
        reconcile_closed_long(intent, payload, expected_instrument_id=77)


def test_close_reconciliation_accepts_recorded_remaining_lot() -> None:
    intent = AuditedIntent(
        intent_id="c" * 64,
        strategy_version="XRP-v1.1-15m",
        candle_timestamp=datetime(2026, 8, 20, 10, tzinfo=UTC),
        action="close-entire-long-position",
        request_path_template=(
            "/api/v1/trading/execution/demo/market-close-orders/positions/123"
        ),
        request_body={"UnitsToDeduct": None},
    )
    payload = _payload({
        "positionID": 124, "instrumentID": 77, "isBuy": True,
        "leverage": 1, "amount": "500", "openRate": "1",
    })
    result = reconcile_closed_long(
        intent,
        payload,
        expected_instrument_id=77,
        allowed_remaining_position_ids=(124,),
    )
    assert result is not None
    assert result.position_id == 123
