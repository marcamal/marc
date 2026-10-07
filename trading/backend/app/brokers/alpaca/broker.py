"""Alpaca Trading API adapter.

Built on the official `alpaca-py` SDK (`alpaca.trading.client.TradingClient`).

The SDK is **synchronous**, so every call is pushed to a worker thread with
`asyncio.to_thread`. Calling it directly from the event loop would stall every
agent, the websocket reader and the API for the duration of the HTTP round
trip. That is the single most important implementation detail in this file.

Everything returned here is converted into ATLAS's own models. No Alpaca SDK
type escapes this module.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.requests import (
    ClosePositionRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

from app.brokers.base import (
    BrokerAdapter,
    BrokerAuthError,
    BrokerCapabilities,
    BrokerConnectionError,
    BrokerError,
    DuplicateOrder,
    OrderRejected,
    OrderRequest,
    normalise_order_status,
)
from app.core.logging import get_logger
from app.models.enums import AssetClass, OrderType, PositionSide, Side, TimeInForce
from app.models.market import MarketClock
from app.models.trading import AccountSnapshot, Order, Position

log = get_logger(__name__)


def _as_float(value: Any, default: float = 0.0) -> float:
    """Alpaca returns many numeric fields as strings or None."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _aware(value: Any) -> datetime | None:
    """Normalise to a timezone-aware UTC datetime.

    Naive datetimes leaking into the models cause comparison errors later, in
    the staleness checks, which is a confusing place to debug a timezone bug.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _enum_value(value: Any) -> str | None:
    """Unwrap an SDK enum (or plain string) into its string value."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def order_from_sdk(raw: Any) -> Order:
    """Convert an Alpaca SDK order into ATLAS's `Order`.

    Module-level rather than a method because the trade-update websocket needs
    it too, and SDK types must not escape this package. Everything downstream
    of here sees only ATLAS models.
    """
    side_value = _enum_value(getattr(raw, "side", "buy")) or "buy"
    type_value = _enum_value(getattr(raw, "order_type", None)) or _enum_value(
        getattr(raw, "type", "market")
    )
    tif_value = _enum_value(getattr(raw, "time_in_force", "day")) or "day"

    try:
        order_type = OrderType(str(type_value).lower())
    except ValueError:
        order_type = OrderType.MARKET
    try:
        tif = TimeInForce(str(tif_value).lower())
    except ValueError:
        tif = TimeInForce.DAY

    return Order(
        id=str(getattr(raw, "id", "")),
        client_order_id=str(getattr(raw, "client_order_id", "") or ""),
        symbol=str(getattr(raw, "symbol", "")),
        side=Side.SELL if side_value == "sell" else Side.BUY,
        order_type=order_type,
        time_in_force=tif,
        status=normalise_order_status(_enum_value(getattr(raw, "status", None))),
        quantity=_as_float(getattr(raw, "qty", 0)),
        filled_quantity=_as_float(getattr(raw, "filled_qty", 0)),
        limit_price=_as_float(getattr(raw, "limit_price", None)) or None,
        stop_price=_as_float(getattr(raw, "stop_price", None)) or None,
        average_fill_price=_as_float(getattr(raw, "filled_avg_price", None)) or None,
        submitted_at=_aware(getattr(raw, "submitted_at", None)),
        filled_at=_aware(getattr(raw, "filled_at", None)),
        canceled_at=_aware(getattr(raw, "canceled_at", None)),
        expired_at=_aware(getattr(raw, "expired_at", None)),
    )


class AlpacaBroker(BrokerAdapter):
    """Order placement, account state and positions via Alpaca."""

    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, paper: bool = True) -> None:
        if not api_key or not secret_key:
            raise BrokerAuthError(
                "Alpaca API key and secret are required. "
                "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file."
            )
        self._paper = paper
        # `paper=True` selects https://paper-api.alpaca.markets. Paper and live
        # keys are distinct, so a mismatch surfaces as a 401/403 on first call.
        self._client = TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)
        self._capabilities: BrokerCapabilities | None = None
        log.info(
            "Alpaca trading client created",
            extra={"endpoint": "paper" if paper else "LIVE"},
        )

    @property
    def is_paper(self) -> bool:
        return self._paper

    # ------------------------------------------------------------------ #
    # error translation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _translate(exc: Exception, context: str) -> BrokerError:
        """Turn an SDK exception into an ATLAS error with an actionable message."""
        if isinstance(exc, APIError):
            status = getattr(exc, "status_code", None)
            message = str(exc)

            if status in (401, 403):
                return BrokerAuthError(
                    f"{context}: Alpaca rejected the credentials (HTTP {status}). "
                    "Check that ALPACA_API_KEY/ALPACA_SECRET_KEY are PAPER keys "
                    "and were generated while the dashboard was in Paper mode. "
                    f"Broker said: {message}"
                )
            # Alpaca returns 422 for a reused client_order_id. Treating that as
            # a duplicate rather than a failure is what makes retries safe.
            if "client_order_id" in message and (
                "exist" in message.lower() or "duplicate" in message.lower()
            ):
                return DuplicateOrder(f"{context}: {message}")
            if status == 429:
                return BrokerConnectionError(
                    f"{context}: Alpaca rate limit hit (HTTP 429). Back off and retry. {message}"
                )
            if status is not None and 400 <= status < 500:
                return OrderRejected(f"{context}: Alpaca refused the request. {message}")
            return BrokerConnectionError(f"{context}: Alpaca API error. {message}")

        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return BrokerConnectionError(f"{context}: network failure talking to Alpaca. {exc}")

        return BrokerError(f"{context}: unexpected failure. {type(exc).__name__}: {exc}")

    async def _call(self, fn: Any, *args: Any, context: str = "alpaca call", **kwargs: Any) -> Any:
        """Run a synchronous SDK method in a worker thread."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:
            raise self._translate(exc, context) from exc

    # ------------------------------------------------------------------ #
    # account
    # ------------------------------------------------------------------ #

    async def get_account(self) -> AccountSnapshot:
        raw = await self._call(self._client.get_account, context="get_account")

        equity = _as_float(getattr(raw, "equity", 0))
        return AccountSnapshot(
            account_id=str(getattr(raw, "id", "unknown")),
            currency=str(getattr(raw, "currency", "USD") or "USD"),
            equity=equity,
            last_equity=_as_float(getattr(raw, "last_equity", 0)),
            cash=_as_float(getattr(raw, "cash", 0)),
            buying_power=_as_float(getattr(raw, "buying_power", 0)),
            multiplier=_as_float(getattr(raw, "multiplier", 1), default=1.0),
            portfolio_value=_as_float(getattr(raw, "portfolio_value", equity), default=equity),
            daytrade_count=_as_int(getattr(raw, "daytrade_count", 0)),
            pattern_day_trader=bool(getattr(raw, "pattern_day_trader", False)),
            trading_blocked=bool(getattr(raw, "trading_blocked", False)),
            transfers_blocked=bool(getattr(raw, "transfers_blocked", False)),
            account_blocked=bool(getattr(raw, "account_blocked", False)),
            shorting_enabled=bool(getattr(raw, "shorting_enabled", False)),
            crypto_enabled=_as_float((getattr(raw, "crypto_status", None) and 1) or 0) > 0
            or str(getattr(raw, "crypto_status", "")).upper() == "ACTIVE",
            options_enabled=_as_int(getattr(raw, "options_trading_level", 0)) > 0,
        )

    async def get_capabilities(self) -> BrokerCapabilities:
        """Ask the account what it can do, rather than assuming.

        Cached after the first successful call: these do not change within a
        session, and the System page polls.
        """
        if self._capabilities is not None:
            return self._capabilities

        caps = BrokerCapabilities(broker_name="alpaca")
        try:
            raw = await self._call(self._client.get_account, context="get_capabilities")
        except BrokerError as exc:
            caps.notes.append(f"Capability detection failed, using safe defaults: {exc}")
            return caps

        multiplier = _as_float(getattr(raw, "multiplier", 1), default=1.0)
        options_level = _as_int(getattr(raw, "options_trading_level", 0))
        crypto_status = str(getattr(raw, "crypto_status", "") or "").upper()

        caps.detected = True
        caps.supports_us_equity = True
        caps.supports_etf = True
        caps.supports_crypto = crypto_status == "ACTIVE"
        caps.supports_options = options_level > 0
        # Alpaca has no direct bond product. Bond exposure goes through ETFs
        # (BND, AGG, TLT). Another broker adapter could change this later.
        caps.supports_direct_bonds = False
        caps.supports_futures = False
        caps.supports_forex = False

        caps.supports_fractional_shares = bool(getattr(raw, "fractional_trading", False))
        caps.supports_notional_orders = caps.supports_fractional_shares
        caps.supports_short_selling = bool(getattr(raw, "shorting_enabled", False))
        caps.margin_enabled = multiplier > 1.0
        caps.max_leverage = multiplier
        caps.pattern_day_trader = bool(getattr(raw, "pattern_day_trader", False))

        if not caps.margin_enabled:
            caps.notes.append(
                "Cash account detected: no margin, no shorting, and sale proceeds "
                "settle before they can be reused (T+1). ATLAS keeps max_leverage at 1.0."
            )
        if not caps.supports_crypto:
            caps.notes.append(
                f"Crypto trading is not active on this account (status: {crypto_status or 'unknown'}). "
                "Availability depends on your country of residence."
            )
        if options_level == 0:
            caps.notes.append(
                "Options trading is not enabled. Options research still works; order "
                "placement is disabled."
            )

        self._capabilities = caps
        log.info(
            "broker capabilities detected",
            extra={
                "crypto": caps.supports_crypto,
                "options": caps.supports_options,
                "margin": caps.margin_enabled,
                "fractional": caps.supports_fractional_shares,
            },
        )
        return caps

    async def get_positions(self) -> list[Position]:
        raw_positions = await self._call(self._client.get_all_positions, context="get_positions")
        positions: list[Position] = []
        for raw in raw_positions or []:
            side_value = _enum_value(getattr(raw, "side", "long")) or "long"
            positions.append(
                Position(
                    symbol=str(getattr(raw, "symbol", "")),
                    quantity=_as_float(getattr(raw, "qty", 0)),
                    side=PositionSide.SHORT if side_value == "short" else PositionSide.LONG,
                    average_entry_price=_as_float(getattr(raw, "avg_entry_price", 0)),
                    current_price=_as_float(getattr(raw, "current_price", 0)) or None,
                    market_value=_as_float(getattr(raw, "market_value", 0)),
                    cost_basis=_as_float(getattr(raw, "cost_basis", 0)),
                    unrealized_pl=_as_float(getattr(raw, "unrealized_pl", 0)),
                    # Alpaca reports this as a fraction (0.05), we display percent.
                    unrealized_pl_pct=_as_float(getattr(raw, "unrealized_plpc", 0)) * 100,
                    asset_class=_enum_value(getattr(raw, "asset_class", "us_equity"))
                    or "us_equity",
                )
            )
        return positions

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #

    def _to_order(self, raw: Any) -> Order:
        return order_from_sdk(raw)

    async def get_orders(
        self, status: str = "open", limit: int = 50, symbols: list[str] | None = None
    ) -> list[Order]:
        try:
            query_status = QueryOrderStatus(status.lower())
        except ValueError:
            query_status = QueryOrderStatus.OPEN

        request = GetOrdersRequest(status=query_status, limit=limit, symbols=symbols)
        raw_orders = await self._call(self._client.get_orders, filter=request, context="get_orders")
        return [self._to_order(o) for o in raw_orders or []]

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        """Used by the idempotency guard after an ambiguous submission."""
        try:
            raw = await self._call(
                self._client.get_order_by_client_id,
                client_order_id,
                context="get_order_by_client_id",
            )
        except BrokerError as exc:
            # A 404 here means "no such order", which is a legitimate answer,
            # not a failure: it tells us the earlier attempt did not land.
            if "404" in str(exc) or "not found" in str(exc).lower():
                return None
            raise
        return self._to_order(raw) if raw else None

    def _build_alpaca_request(self, request: OrderRequest) -> Any:
        """Map our OrderRequest onto the right SDK request object."""
        side = AlpacaOrderSide.SELL if request.side.lower() == "sell" else AlpacaOrderSide.BUY
        try:
            tif = AlpacaTimeInForce(request.time_in_force.lower())
        except ValueError:
            tif = AlpacaTimeInForce.DAY

        common: dict[str, Any] = {
            "symbol": request.symbol,
            "qty": request.quantity,
            "side": side,
            "time_in_force": tif,
            "client_order_id": request.client_order_id,
            "extended_hours": request.extended_hours,
        }

        # Attach bracket legs when the caller supplied them. Alpaca infers
        # order_class=bracket from the presence of both legs.
        if request.take_profit_price is not None:
            common["take_profit"] = TakeProfitRequest(limit_price=request.take_profit_price)
        if request.stop_loss_price is not None:
            common["stop_loss"] = StopLossRequest(
                stop_price=request.stop_loss_price,
                limit_price=request.stop_loss_limit_price,
            )

        order_type = request.order_type.lower()
        if order_type == "limit":
            return LimitOrderRequest(limit_price=request.limit_price, **common)
        if order_type == "stop":
            return StopOrderRequest(stop_price=request.stop_price, **common)
        if order_type == "stop_limit":
            return StopLimitOrderRequest(
                stop_price=request.stop_price, limit_price=request.limit_price, **common
            )
        return MarketOrderRequest(**common)

    async def submit_order(self, request: OrderRequest) -> Order:
        """Send one order.

        Only ever called by the Execution Agent. The `client_order_id` on the
        request is what makes a retry safe.
        """
        alpaca_request = self._build_alpaca_request(request)
        raw = await self._call(
            self._client.submit_order,
            order_data=alpaca_request,
            context=f"submit_order {request.symbol}",
        )
        order = self._to_order(raw)
        log.info(
            "order submitted to Alpaca",
            extra={
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": order.quantity,
                "broker_order_id": order.id,
                "client_order_id": order.client_order_id,
                "status": order.status.value,
            },
        )
        return order

    async def cancel_order(self, order_id: str) -> None:
        await self._call(
            self._client.cancel_order_by_id, order_id, context=f"cancel_order {order_id}"
        )

    async def cancel_all_orders(self) -> int:
        responses = await self._call(self._client.cancel_orders, context="cancel_all_orders")
        count = len(responses or [])
        log.warning("cancelled all open orders", extra={"count": count})
        return count

    async def close_position(self, symbol: str) -> Order | None:
        raw = await self._call(
            self._client.close_position,
            symbol,
            close_options=ClosePositionRequest(percentage="100"),
            context=f"close_position {symbol}",
        )
        return self._to_order(raw) if raw else None

    async def close_all_positions(self, cancel_orders: bool = True) -> int:
        responses = await self._call(
            self._client.close_all_positions,
            cancel_orders=cancel_orders,
            context="close_all_positions",
        )
        count = len(responses or [])
        log.critical("flattened all positions", extra={"count": count})
        return count

    # ------------------------------------------------------------------ #
    # market session
    # ------------------------------------------------------------------ #

    async def get_clock(self) -> MarketClock:
        raw = await self._call(self._client.get_clock, context="get_clock")
        return MarketClock(
            timestamp=_aware(getattr(raw, "timestamp", None)) or datetime.now(UTC),
            is_open=bool(getattr(raw, "is_open", False)),
            next_open=_aware(getattr(raw, "next_open", None)),
            next_close=_aware(getattr(raw, "next_close", None)),
        )

    async def get_asset_class(self, symbol: str) -> AssetClass:
        """Ask Alpaca what this symbol is, and whether it is tradable."""
        try:
            raw = await self._call(self._client.get_asset, symbol, context=f"get_asset {symbol}")
        except BrokerError:
            return AssetClass.UNSUPPORTED

        if raw is None or not bool(getattr(raw, "tradable", False)):
            return AssetClass.UNSUPPORTED

        value = _enum_value(getattr(raw, "asset_class", None))
        try:
            return AssetClass(str(value))
        except ValueError:
            return AssetClass.UNSUPPORTED
