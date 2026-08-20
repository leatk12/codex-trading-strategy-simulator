"""Strictly read-only access to eToro's Demo and market-data APIs."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class EtoroDemoError(RuntimeError):
    """Raised for unsafe configuration or a failed read-only API request."""


JsonObject = Mapping[str, Any]
ReadTransport = Callable[[str, Mapping[str, str], float], JsonObject]


@dataclass(frozen=True, slots=True, repr=False)
class EtoroCredentials:
    """Credentials whose representation deliberately never reveals secrets."""

    public_key: str
    private_key: str

    def __post_init__(self) -> None:
        if not self.public_key.strip() or not self.private_key.strip():
            raise EtoroDemoError("both eToro public and private keys are required")

    @classmethod
    def from_environment(cls) -> "EtoroCredentials":
        public_key = os.environ.get("ETORO_PUBLIC_KEY", "")
        private_key = os.environ.get("ETORO_PRIVATE_KEY", "")
        if not public_key or not private_key:
            raise EtoroDemoError(
                "set ETORO_PUBLIC_KEY and ETORO_PRIVATE_KEY in the current process"
            )
        return cls(public_key, private_key)

    def __repr__(self) -> str:
        return "EtoroCredentials(public_key=<redacted>, private_key=<redacted>)"


@dataclass(frozen=True, slots=True)
class EtoroDemoPortfolioSummary:
    currency: str
    credit: Decimal
    available_cash: Decimal
    total_invested: Decimal
    unrealized_profit_loss: Decimal
    equity: Decimal
    open_position_count: int
    pending_order_count: int


class EtoroDemoReadOnlyClient:
    """Allow GET reads from Demo/market-data paths and nothing else."""

    ORIGIN = "https://public-api.etoro.com"
    DEMO_PORTFOLIO_PATH = "/api/v1/trading/info/demo/portfolio"
    DEMO_PNL_PATH = "/api/v1/trading/info/demo/pnl"
    MARKET_DATA_PREFIX = "/api/v1/market-data/"

    def __init__(
        self,
        credentials: EtoroCredentials,
        *,
        transport: ReadTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise EtoroDemoError("timeout_seconds must be positive")
        self._credentials = credentials
        self._transport = transport or _read_json
        self._timeout_seconds = timeout_seconds

    def demo_portfolio(self) -> JsonObject:
        return self._get(self.DEMO_PORTFOLIO_PATH)

    def demo_pnl(self) -> JsonObject:
        return self._get(self.DEMO_PNL_PATH)

    def demo_summary(
        self, response: JsonObject | None = None
    ) -> EtoroDemoPortfolioSummary:
        """Calculate official USD account measures from the Demo P&L payload."""

        response = self.demo_pnl() if response is None else response
        portfolio = response.get("clientPortfolio")
        if not isinstance(portfolio, Mapping):
            raise EtoroDemoError("eToro response is missing clientPortfolio")

        credit = _decimal(portfolio.get("credit"), "credit")
        positions = _objects(portfolio.get("positions", []), "positions")
        orders_for_open = _objects(
            portfolio.get("ordersForOpen", []), "ordersForOpen"
        )
        orders = _objects(portfolio.get("orders", []), "orders")
        mirrors = _objects(portfolio.get("mirrors", []), "mirrors")
        manual_pending = [
            order
            for order in orders_for_open
            if _decimal(order.get("mirrorID", 0), "ordersForOpen.mirrorID") == 0
        ]

        pending_amount = _sum_field(manual_pending, "amount", "ordersForOpen")
        pending_amount += _sum_field(orders, "amount", "orders")
        available_cash = credit - pending_amount

        total_invested = _sum_field(positions, "amount", "positions")
        unrealized = _sum_nested_pnl(positions, "positions")
        mirrored_position_count = 0
        for mirror in mirrors:
            mirror_positions = _objects(
                mirror.get("positions", []), "mirrors.positions"
            )
            mirrored_position_count += len(mirror_positions)
            total_invested += _sum_field(
                mirror_positions, "amount", "mirrors.positions"
            )
            closed_profit = _decimal(
                mirror.get("closedPositionsNetProfit", 0),
                "mirrors.closedPositionsNetProfit",
            )
            total_invested += _decimal(
                mirror.get("availableAmount", 0), "mirrors.availableAmount"
            ) - closed_profit
            unrealized += _sum_nested_pnl(
                mirror_positions, "mirrors.positions"
            ) + closed_profit

        total_invested += pending_amount
        total_invested += _sum_field(
            manual_pending, "totalExternalCosts", "ordersForOpen", default=0
        )
        equity = available_cash + total_invested + unrealized
        return EtoroDemoPortfolioSummary(
            currency="USD",
            credit=credit,
            available_cash=available_cash,
            total_invested=total_invested,
            unrealized_profit_loss=unrealized,
            equity=equity,
            open_position_count=len(positions) + mirrored_position_count,
            pending_order_count=len(manual_pending) + len(orders),
        )

    def market_data(
        self, resource: str, parameters: Mapping[str, str] | None = None
    ) -> JsonObject:
        clean_resource = resource.strip("/")
        if not clean_resource or ".." in clean_resource.split("/"):
            raise EtoroDemoError("market-data resource is invalid")
        path = f"{self.MARKET_DATA_PREFIX}{clean_resource}"
        if parameters:
            path = f"{path}?{urlencode(dict(parameters))}"
        return self._get(path)

    def _get(self, path: str) -> JsonObject:
        url = f"{self.ORIGIN}{path}"
        self._validate_url(url)
        # eToro's portal labels these Public/Private. Its HTTP documentation
        # names the corresponding headers API Key/User Key respectively.
        headers = {
            "x-api-key": self._credentials.public_key,
            "x-user-key": self._credentials.private_key,
            "x-request-id": str(uuid4()),
            "accept": "application/json",
            "user-agent": "trading-strategy-simulator/0.11.1 (read-only demo client)",
        }
        try:
            return self._transport(url, headers, self._timeout_seconds)
        except EtoroDemoError:
            raise
        except Exception as error:
            raise EtoroDemoError(
                f"eToro read failed ({type(error).__name__}); credentials were redacted"
            ) from error

    @classmethod
    def _validate_url(cls, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "public-api.etoro.com":
            raise EtoroDemoError("only the official HTTPS eToro API origin is allowed")
        path = parsed.path
        allowed = path in {cls.DEMO_PORTFOLIO_PATH, cls.DEMO_PNL_PATH} or path.startswith(
            cls.MARKET_DATA_PREFIX
        )
        forbidden = "/execution" in path or "/real/" in path
        if not allowed or forbidden:
            raise EtoroDemoError(f"read-only adapter blocked unsafe path: {path}")


def _read_json(url: str, headers: Mapping[str, str], timeout: float) -> JsonObject:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        opener = build_opener(_RejectRedirects())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as error:
        raw_error = error.read(16_384).decode("utf-8", errors="replace")
        detail = _safe_error_detail(
            raw_error,
            secrets=(headers.get("x-api-key", ""), headers.get("x-user-key", "")),
        )
        suffix = "" if detail is None else f": {detail}"
        raise EtoroDemoError(f"eToro returned HTTP {error.code}{suffix}") from error
    except URLError as error:
        raise EtoroDemoError("could not reach the eToro API") from error
    try:
        content = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EtoroDemoError("eToro returned an invalid JSON response") from error
    if not isinstance(content, dict):
        raise EtoroDemoError("eToro response must be a JSON object")
    return content


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # type: ignore[no-untyped-def]
        return None


def _safe_error_detail(
    raw: str, *, secrets: tuple[str, str]
) -> str | None:
    """Extract only a short API error description and redact credentials."""

    raw = raw[:16_384]
    try:
        content = json.loads(raw)
    except json.JSONDecodeError:
        content = None
    if isinstance(content, dict):
        allowed = ("errorCode", "code", "message", "error", "title", "detail")
        pieces = [
            f"{name}={content[name]}"
            for name in allowed
            if name in content and isinstance(content[name], (str, int))
        ]
        detail = "; ".join(pieces)
    else:
        detail = raw.strip()
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "<redacted>")
    detail = " ".join(detail.split())
    return detail[:400] or None


def _objects(value: object, field_name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise EtoroDemoError(f"eToro field {field_name} must be a list of objects")
    return value


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise EtoroDemoError(f"eToro field {field_name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise EtoroDemoError(f"eToro field {field_name} must be numeric") from error
    if not result.is_finite():
        raise EtoroDemoError(f"eToro field {field_name} must be finite")
    return result


def _sum_field(
    objects: list[Mapping[str, Any]],
    field: str,
    collection: str,
    *,
    default: object | None = None,
) -> Decimal:
    return sum(
        (
            _decimal(
                item.get(field, default) if default is not None else item.get(field),
                f"{collection}.{field}",
            )
            for item in objects
        ),
        Decimal("0"),
    )


def _sum_nested_pnl(
    positions: list[Mapping[str, Any]], collection: str
) -> Decimal:
    total = Decimal("0")
    for position in positions:
        pnl = position.get("unrealizedPnL", {})
        if not isinstance(pnl, Mapping):
            raise EtoroDemoError(
                f"eToro field {collection}.unrealizedPnL must be an object"
            )
        total += _decimal(pnl.get("pnL", 0), f"{collection}.unrealizedPnL.pnL")
    return total
