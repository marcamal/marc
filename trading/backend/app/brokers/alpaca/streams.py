"""Alpaca websocket streams: market data and trade updates.

**Threading model — read this before changing anything here.**

`alpaca-py`'s stream classes expose a synchronous `run()` that calls
`asyncio.run(self._run_forever())`. That creates a *new event loop*, so it
cannot be awaited from inside ATLAS's loop. Two options existed:

*   call the private `_run_forever()` coroutine in our own loop, or
*   run the public `run()` in a worker thread and bridge messages back.

This module takes the second option. It uses only public SDK API, so an SDK
upgrade cannot silently break the stream, and the cost is a single
`loop.call_soon_threadsafe` hop per message — negligible next to network
latency.

The consequence is a hard rule: **SDK handlers execute on the stream thread,
not on the ATLAS event loop.** `asyncio.Queue.put_nowait` is not thread-safe,
so handlers must never touch the event bus directly. They marshal to the main
loop via `call_soon_threadsafe` and nothing else. Every handler below follows
that pattern.

Reconnection is layered: the SDK retries internally, and this module
supervises on top, so that `run()` returning (connection dead, auth revoked,
subscription limit hit) restarts the stream with exponential backoff instead
of leaving ATLAS silently blind.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from alpaca.data.live.crypto import CryptoDataStream
from alpaca.data.live.stock import StockDataStream
from alpaca.trading.stream import TradingStream

from app.core.logging import get_logger
from app.models.enums import ConnectionState
from app.models.market import Bar, Quote, Trade

log = get_logger(__name__)

#: Called on the ATLAS event loop with each normalised payload.
BarCallback = Callable[[Bar], None]
QuoteCallback = Callable[[Quote], None]
TradeCallback = Callable[[Trade], None]
TradeUpdateCallback = Callable[[dict[str, Any]], None]
StateCallback = Callable[[str, ConnectionState, str], None]


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _opt_float(value: Any) -> float | None:
    """Alpaca sends these as strings, or omits them entirely."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class _SupervisedStream:
    """Runs one SDK stream in a thread and restarts it if it dies.

    Subclasses provide `_create_stream()` and `_wire_handlers()`.
    """

    service_name = "stream"

    def __init__(
        self,
        on_state_change: StateCallback | None = None,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
    ) -> None:
        self._on_state_change = on_state_change
        self._initial_delay = initial_delay
        self._max_delay = max_delay

        self._stream: Any = None
        self._thread: threading.Thread | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown = threading.Event()

        self.state = ConnectionState.DISCONNECTED
        self.connected_at: datetime | None = None
        self.last_message_at: datetime | None = None
        self.message_count = 0
        self.reconnect_count = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------ #
    # to be provided by subclasses
    # ------------------------------------------------------------------ #

    def _create_stream(self) -> Any:  # pragma: no cover - subclass duty
        raise NotImplementedError

    def _wire_handlers(self, stream: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def _has_subscriptions(self) -> bool:
        """The SDK's run loop idles until something is subscribed."""
        return True

    # ------------------------------------------------------------------ #
    # thread-safe bridge
    # ------------------------------------------------------------------ #

    def _dispatch(self, fn: Callable[..., None] | None, *args: Any) -> None:
        """Hand a payload to the ATLAS loop from the stream thread.

        This is the ONLY sanctioned way for an SDK handler to deliver data.
        """
        if fn is None or self._loop is None or self._loop.is_closed():
            return
        self.message_count += 1
        self.last_message_at = datetime.now(UTC)
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            # The loop shut down between the check and the call. Dropping a
            # message during shutdown is correct behaviour.
            pass

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        self.state = state
        if state is ConnectionState.CONNECTED:
            self.connected_at = datetime.now(UTC)
        if self._on_state_change and self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(
                    self._on_state_change, self.service_name, state, detail
                )
            except RuntimeError:
                pass

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._supervisor is not None and not self._supervisor.done():
            return
        self._loop = asyncio.get_running_loop()
        self._shutdown.clear()
        self._supervisor = asyncio.create_task(
            self._supervise(), name=f"stream-supervisor:{self.service_name}"
        )

    async def _supervise(self) -> None:
        """Keep the stream alive, with exponential backoff between attempts."""
        delay = self._initial_delay
        while not self._shutdown.is_set():
            if not self._has_subscriptions():
                # Nothing to stream yet. Poll rather than spin.
                await asyncio.sleep(1.0)
                continue
            try:
                self._set_state(ConnectionState.CONNECTING)
                self._stream = self._create_stream()
                self._wire_handlers(self._stream)

                self._thread = threading.Thread(
                    target=self._run_blocking,
                    name=f"alpaca-{self.service_name}",
                    daemon=True,
                )
                self._thread.start()
                self._set_state(ConnectionState.CONNECTED, "websocket running")
                log.info("%s stream started", self.service_name)

                # The SDK handles its own retries inside run(). If run()
                # returns, the stream is genuinely finished.
                while self._thread.is_alive() and not self._shutdown.is_set():
                    await asyncio.sleep(0.5)

                if self._shutdown.is_set():
                    break

                self.last_error = "stream loop exited"
                log.warning("%s stream exited, will reconnect", self.service_name)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("%s stream crashed", self.service_name)
                self._set_state(ConnectionState.ERROR, self.last_error)

            if self._shutdown.is_set():
                break

            self.reconnect_count += 1
            self._set_state(
                ConnectionState.RECONNECTING,
                f"retry in {delay:.0f}s (attempt {self.reconnect_count})",
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, self._max_delay)

        self._set_state(ConnectionState.DISCONNECTED, "stopped")

    def _run_blocking(self) -> None:
        """Body of the stream thread."""
        try:
            self._stream.run()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error("%s stream thread failed: %s", self.service_name, exc, exc_info=True)

    async def stop(self) -> None:
        self._shutdown.set()

        if self._stream is not None:
            try:
                # stop() marshals into the stream's own loop. It raises if the
                # stream never started, which is harmless here.
                await asyncio.to_thread(self._stream.stop)
            except Exception as exc:
                log.debug("%s stream stop() raised: %s", self.service_name, exc)

        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
        self._supervisor = None

        if self._thread is not None and self._thread.is_alive():
            # Daemon thread: joined briefly, then abandoned so shutdown cannot
            # hang on an unresponsive socket.
            await asyncio.to_thread(self._thread.join, 5.0)
        self._thread = None
        self._stream = None
        self._set_state(ConnectionState.DISCONNECTED, "stopped")
        log.info("%s stream stopped", self.service_name)

    @property
    def is_connected(self) -> bool:
        return self.state is ConnectionState.CONNECTED

    @property
    def seconds_since_last_message(self) -> float | None:
        if self.last_message_at is None:
            return None
        return (datetime.now(UTC) - self.last_message_at).total_seconds()

    def status(self) -> dict[str, Any]:
        return {
            "service": self.service_name,
            "state": self.state.value,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_message_at": (self.last_message_at.isoformat() if self.last_message_at else None),
            "seconds_since_last_message": self.seconds_since_last_message,
            "message_count": self.message_count,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
        }


class AlpacaMarketStream(_SupervisedStream):
    """The single market-data websocket for the whole application.

    The brief was explicit: one connection, not one per agent. Alpaca's free
    plan permits exactly one concurrent connection per account, so a
    per-agent design would not merely be wasteful, it would fail outright.
    Normalised bars/quotes/trades go onto the event bus; every agent reads
    from there.
    """

    service_name = "alpaca_market_stream"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        feed: str = "iex",
        max_symbols: int = 30,
        on_bar: BarCallback | None = None,
        on_quote: QuoteCallback | None = None,
        on_trade: TradeCallback | None = None,
        on_state_change: StateCallback | None = None,
        subscribe_bars: bool = True,
        subscribe_quotes: bool = True,
        subscribe_trades: bool = False,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
    ) -> None:
        super().__init__(on_state_change, initial_delay, max_delay)
        self._api_key = api_key
        self._secret_key = secret_key
        self._feed = feed
        self._max_symbols = max_symbols
        self._on_bar = on_bar
        self._on_quote = on_quote
        self._on_trade = on_trade
        self._want_bars = subscribe_bars
        self._want_quotes = subscribe_quotes
        self._want_trades = subscribe_trades
        self._symbols: list[str] = []
        self.rejected_symbols: list[str] = []

    def set_symbols(self, symbols: list[str]) -> list[str]:
        """Choose what to stream, respecting the plan's symbol budget.

        The free Alpaca plan allows 30 symbols on one connection. Rather than
        let the broker reject the whole subscription, ATLAS truncates the list,
        records what was dropped, and reports it on the System page.

        Returns the symbols that will actually be streamed.
        """
        unique: list[str] = []
        for symbol in symbols:
            if symbol not in unique:
                unique.append(symbol)

        accepted = unique[: self._max_symbols]
        self.rejected_symbols = unique[self._max_symbols :]
        if self.rejected_symbols:
            log.warning(
                "stream symbol budget exceeded, truncating subscription",
                extra={
                    "limit": self._max_symbols,
                    "requested": len(unique),
                    "dropped": self.rejected_symbols,
                },
            )
        self._symbols = accepted
        return accepted

    def _has_subscriptions(self) -> bool:
        return bool(self._symbols)

    def _create_stream(self) -> Any:
        return StockDataStream(
            api_key=self._api_key,
            secret_key=self._secret_key,
            feed=self._feed,  # type: ignore[arg-type]
        )

    def _wire_handlers(self, stream: Any) -> None:
        symbols = tuple(self._symbols)
        if not symbols:
            return

        # Each of these runs on the stream thread. They do nothing but
        # normalise and hand off; see the module docstring.
        async def handle_bar(data: Any) -> None:
            self._dispatch(
                self._on_bar,
                Bar(
                    symbol=str(data.symbol),
                    timestamp=_aware(getattr(data, "timestamp", None)),
                    open=float(data.open),
                    high=float(data.high),
                    low=float(data.low),
                    close=float(data.close),
                    volume=float(data.volume),
                    trade_count=(
                        int(data.trade_count) if getattr(data, "trade_count", None) else None
                    ),
                    vwap=float(data.vwap) if getattr(data, "vwap", None) else None,
                ),
            )

        async def handle_quote(data: Any) -> None:
            self._dispatch(
                self._on_quote,
                Quote(
                    symbol=str(data.symbol),
                    timestamp=_aware(getattr(data, "timestamp", None)),
                    bid_price=float(getattr(data, "bid_price", 0) or 0),
                    bid_size=float(getattr(data, "bid_size", 0) or 0),
                    ask_price=float(getattr(data, "ask_price", 0) or 0),
                    ask_size=float(getattr(data, "ask_size", 0) or 0),
                ),
            )

        async def handle_trade(data: Any) -> None:
            self._dispatch(
                self._on_trade,
                Trade(
                    symbol=str(data.symbol),
                    timestamp=_aware(getattr(data, "timestamp", None)),
                    price=float(data.price),
                    size=float(data.size),
                    exchange=str(getattr(data, "exchange", "") or "") or None,
                ),
            )

        if self._want_bars:
            stream.subscribe_bars(handle_bar, *symbols)
        if self._want_quotes:
            stream.subscribe_quotes(handle_quote, *symbols)
        if self._want_trades:
            stream.subscribe_trades(handle_trade, *symbols)

        log.info(
            "market stream subscriptions wired",
            extra={
                "symbols": len(symbols),
                "bars": self._want_bars,
                "quotes": self._want_quotes,
                "trades": self._want_trades,
                "feed": self._feed,
            },
        )

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update(
            {
                "feed": self._feed,
                "symbol_count": len(self._symbols),
                "symbols": self._symbols,
                "symbol_limit": self._max_symbols,
                "rejected_symbols": self.rejected_symbols,
            }
        )
        return base


class AlpacaCryptoStream(_SupervisedStream):
    """Separate connection for crypto pairs.

    Crypto streams on a different Alpaca endpoint and needs no data
    subscription, so it cannot share the equity connection.
    """

    service_name = "alpaca_crypto_stream"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        on_bar: BarCallback | None = None,
        on_quote: QuoteCallback | None = None,
        on_state_change: StateCallback | None = None,
    ) -> None:
        super().__init__(on_state_change)
        self._api_key = api_key
        self._secret_key = secret_key
        self._on_bar = on_bar
        self._on_quote = on_quote
        self._symbols: list[str] = []

    def set_symbols(self, symbols: list[str]) -> list[str]:
        self._symbols = [s for s in dict.fromkeys(symbols) if "/" in s]
        return self._symbols

    def _has_subscriptions(self) -> bool:
        return bool(self._symbols)

    def _create_stream(self) -> Any:
        return CryptoDataStream(api_key=self._api_key, secret_key=self._secret_key)

    def _wire_handlers(self, stream: Any) -> None:
        symbols = tuple(self._symbols)
        if not symbols:
            return

        async def handle_bar(data: Any) -> None:
            self._dispatch(
                self._on_bar,
                Bar(
                    symbol=str(data.symbol),
                    timestamp=_aware(getattr(data, "timestamp", None)),
                    open=float(data.open),
                    high=float(data.high),
                    low=float(data.low),
                    close=float(data.close),
                    volume=float(data.volume),
                    vwap=float(data.vwap) if getattr(data, "vwap", None) else None,
                ),
            )

        async def handle_quote(data: Any) -> None:
            self._dispatch(
                self._on_quote,
                Quote(
                    symbol=str(data.symbol),
                    timestamp=_aware(getattr(data, "timestamp", None)),
                    bid_price=float(getattr(data, "bid_price", 0) or 0),
                    bid_size=float(getattr(data, "bid_size", 0) or 0),
                    ask_price=float(getattr(data, "ask_price", 0) or 0),
                    ask_size=float(getattr(data, "ask_size", 0) or 0),
                ),
            )

        stream.subscribe_bars(handle_bar, *symbols)
        stream.subscribe_quotes(handle_quote, *symbols)


class AlpacaTradeStream(_SupervisedStream):
    """Order and fill updates pushed from the broker.

    This is how ATLAS learns about fills, partial fills, cancellations and
    rejections without polling. It is also the safety net that catches a fill
    on an order whose HTTP response was lost.
    """

    service_name = "alpaca_trade_stream"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        on_trade_update: TradeUpdateCallback | None = None,
        on_state_change: StateCallback | None = None,
    ) -> None:
        super().__init__(on_state_change)
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._on_trade_update = on_trade_update

    def _create_stream(self) -> Any:
        return TradingStream(api_key=self._api_key, secret_key=self._secret_key, paper=self._paper)

    def _wire_handlers(self, stream: Any) -> None:
        # Imported here to keep the module import graph acyclic.
        from app.brokers.alpaca.broker import order_from_sdk

        async def handle_update(data: Any) -> None:
            # The SDK order is converted to an ATLAS `Order` right here, so no
            # vendor type crosses out of app.brokers. The Execution Agent needs
            # the event name ("fill", "partial_fill", "canceled") alongside it.
            raw_order = getattr(data, "order", None)
            payload = {
                "event": str(getattr(data, "event", "") or "update"),
                "order": order_from_sdk(raw_order) if raw_order is not None else None,
                "timestamp": _aware(getattr(data, "timestamp", None)),
                "price": _opt_float(getattr(data, "price", None)),
                "qty": _opt_float(getattr(data, "qty", None)),
                "position_qty": _opt_float(getattr(data, "position_qty", None)),
            }
            self._dispatch(self._on_trade_update, payload)

        stream.subscribe_trade_updates(handle_update)
        log.info("trade update stream subscriptions wired")
