"""Broker and market-data interfaces.

Alpaca is the only implementation today. The interfaces exist so that adding
Interactive Brokers or a crypto exchange later is an additive change rather
than a rewrite — and so that tests can run against `SimulatedBroker` without
touching the network.

**Capability detection.** ATLAS never hard-codes what a given account can do.
Margin, shorting, crypto, options and fractional shares all depend on the
account type, the broker, and the operator's country of residence. A German
retail account does not get the same product set as a US one, and that set
changes over time. So `get_capabilities()` asks the broker and reports what is
actually available; the UI shows `UNSUPPORTED BY CURRENT BROKER` for the rest
rather than offering something that will fail at order time.
"""

from __future__ import annotations

import abc
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import AssetClass, OrderStatus
from app.models.market import Bar, MarketClock, Quote, Snapshot, Trade
from app.models.trading import AccountSnapshot, Order, Position


class BrokerCapabilities(BaseModel):
    """What this broker/account combination can actually do.

    Populated from the live account where possible, conservatively guessed
    only where the API gives us nothing.
    """

    broker_name: str
    #: True once the values below came from a real API response rather than
    #: defaults. The System page shows "detected" vs "assumed".
    detected: bool = False

    # --- asset classes -----------------------------------------------------
    supports_us_equity: bool = True
    supports_etf: bool = True
    supports_crypto: bool = False
    supports_options: bool = False
    #: Direct bond trading. Alpaca does not offer it; bond *ETFs* work fine,
    #: which is why the UI distinguishes the two.
    supports_direct_bonds: bool = False
    supports_futures: bool = False
    supports_forex: bool = False

    # --- order features ----------------------------------------------------
    supports_fractional_shares: bool = False
    supports_bracket_orders: bool = True
    supports_oco_orders: bool = True
    supports_trailing_stops: bool = True
    supports_short_selling: bool = False
    supports_extended_hours: bool = True
    supports_notional_orders: bool = False

    # --- account -----------------------------------------------------------
    margin_enabled: bool = False
    max_leverage: float = 1.0
    pattern_day_trader: bool = False
    #: Alpaca restricts non-PDT margin accounts to 3 day trades per 5 days.
    day_trades_remaining: int | None = None

    # --- data --------------------------------------------------------------
    data_feed: str = "iex"
    max_stream_symbols: int = 30
    has_paid_data_plan: bool = False
    supports_news: bool = True

    notes: list[str] = Field(default_factory=list)

    def unsupported_summary(self) -> list[str]:
        """Labels for the dashboard's "not available" list."""
        missing: list[str] = []
        if not self.supports_crypto:
            missing.append("Crypto trading")
        if not self.supports_options:
            missing.append("Options trading")
        if not self.supports_direct_bonds:
            missing.append("Direct bonds (use bond ETFs instead)")
        if not self.supports_short_selling:
            missing.append("Short selling")
        if not self.supports_fractional_shares:
            missing.append("Fractional shares")
        if not self.margin_enabled:
            missing.append("Margin / leverage")
        return missing


class OrderRequest(BaseModel):
    """A broker-agnostic order instruction.

    Produced ONLY by the Execution Agent, from an approved `TradeProposal`.
    `client_order_id` is mandatory and is what makes submission idempotent: a
    retry after a network timeout reuses the same id, so the broker recognises
    it as the same order rather than opening a second position.
    """

    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    time_in_force: str = "day"
    limit_price: float | None = None
    stop_price: float | None = None
    client_order_id: str
    extended_hours: bool = False
    #: Bracket legs, when the broker supports them.
    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    stop_loss_limit_price: float | None = None


class BrokerError(RuntimeError):
    """Base class for broker failures."""


class BrokerAuthError(BrokerError):
    """Credentials missing, wrong, or pointed at the wrong environment.

    The most common cause is live keys in a paper config or vice versa.
    """


class BrokerConnectionError(BrokerError):
    """Network-level failure. Retryable."""


class OrderRejected(BrokerError):
    """The broker refused the order. Not retryable without a change."""


class DuplicateOrder(BrokerError):
    """An order with this `client_order_id` already exists.

    Treated as success by the Execution Agent — it means a previous attempt
    actually got through, and resubmitting would double the position.
    """


class BrokerAdapter(abc.ABC):
    """The order and account interface.

    Implementations must be safe to call from async code. The Alpaca SDK is
    synchronous, so `AlpacaBroker` offloads to a thread pool.
    """

    name: str = "abstract"

    # --- account -----------------------------------------------------------

    @abc.abstractmethod
    async def get_account(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    async def get_capabilities(self) -> BrokerCapabilities: ...

    @abc.abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    async def get_orders(
        self, status: str = "open", limit: int = 50, symbols: list[str] | None = None
    ) -> list[Order]: ...

    @abc.abstractmethod
    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        """Look up an order by OUR id.

        Central to idempotency: after an ambiguous network failure the
        Execution Agent asks "did my order actually land?" using this.
        """

    # --- orders ------------------------------------------------------------

    @abc.abstractmethod
    async def submit_order(self, request: OrderRequest) -> Order: ...

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abc.abstractmethod
    async def cancel_all_orders(self) -> int:
        """Cancel every open order. Returns how many were cancelled."""

    @abc.abstractmethod
    async def close_position(self, symbol: str) -> Order | None:
        """Flatten one position at market. Used only by the kill switch."""

    @abc.abstractmethod
    async def close_all_positions(self, cancel_orders: bool = True) -> int: ...

    # --- market session ----------------------------------------------------

    @abc.abstractmethod
    async def get_clock(self) -> MarketClock: ...

    @abc.abstractmethod
    async def get_asset_class(self, symbol: str) -> AssetClass:
        """Which asset class a symbol belongs to, or UNSUPPORTED."""

    # --- health ------------------------------------------------------------

    async def ping(self) -> bool:
        """Cheap liveness check. Default implementation hits the clock."""
        try:
            await self.get_clock()
            return True
        except Exception:
            return False


class MarketDataProvider(abc.ABC):
    """Historical and latest-snapshot market data (REST, not streaming)."""

    name: str = "abstract"

    @abc.abstractmethod
    async def get_bars(
        self,
        symbols: list[str],
        timeframe: str = "5Min",
        limit: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, list[Bar]]:
        """Historical bars, keyed by symbol and ordered oldest first."""

    @abc.abstractmethod
    async def get_latest_quote(self, symbol: str) -> Quote | None: ...

    @abc.abstractmethod
    async def get_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    @abc.abstractmethod
    async def get_latest_trade(self, symbol: str) -> Trade | None: ...

    @abc.abstractmethod
    async def get_snapshots(self, symbols: list[str]) -> dict[str, Snapshot]: ...


#: Maps broker-specific status strings onto our `OrderStatus`.
#: Anything unrecognised becomes UNKNOWN rather than being guessed at, so a
#: new broker state cannot be mistaken for "filled".
def normalise_order_status(raw: str | None) -> OrderStatus:
    if not raw:
        return OrderStatus.UNKNOWN
    try:
        return OrderStatus(str(raw).lower())
    except ValueError:
        return OrderStatus.UNKNOWN
