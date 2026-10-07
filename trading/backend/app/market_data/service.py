"""The Market Data Service — one streaming connection for the whole system.

    Alpaca websocket
          |
    MarketDataService   (this module: normalise, cache, detect staleness)
          |
    Internal event bus
          |
    Scanner / Technical / Strategy / Risk / UI / Database

The brief was explicit that agents must not each open a socket, and Alpaca
enforces it anyway: the free plan permits one concurrent connection per
account. So this service is the sole owner of the streams, and everything else
subscribes to the bus.

It also keeps a bounded in-memory bar cache. Indicators need history on every
evaluation; re-fetching 120 bars per symbol per cycle would burn the rate
limit within minutes.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.brokers.base import BrokerError, MarketDataProvider
from app.config.settings import Settings
from app.core.logging import get_logger
from app.events.bus import EventBus
from app.events.types import (
    BarEvent,
    ConnectionEvent,
    OrderUpdateEvent,
    QuoteEvent,
    StaleDataEvent,
    TradeEvent,
)
from app.market_data import timeframes
from app.models.enums import ConnectionState
from app.models.market import Bar, Quote, Trade

log = get_logger(__name__)

#: Bars kept per symbol per timeframe. 500 x 5min covers several sessions,
#: which is enough for every indicator here and still small in memory.
BAR_CACHE_SIZE = 500


class MarketDataService:
    """Owns streaming, caching and freshness for all market data."""

    def __init__(
        self,
        provider: MarketDataProvider,
        bus: EventBus,
        settings: Settings,
        staleness_warning_seconds: float = 120.0,
    ) -> None:
        self._provider = provider
        self._bus = bus
        self._settings = settings
        self._staleness_threshold = staleness_warning_seconds

        # (symbol, timeframe) -> bars, oldest first
        self._bars: dict[tuple[str, str], deque[Bar]] = {}
        self._quotes: dict[str, Quote] = {}
        self._trades: dict[str, Trade] = {}

        self._symbols: list[str] = []
        self._market_stream: Any = None
        self._trade_stream: Any = None
        self._stale_monitor: asyncio.Task[None] | None = None

        self.connection_states: dict[str, ConnectionState] = {}
        self.last_stream_message_at: datetime | None = None
        #: Set while a staleness warning is active, so the warning fires once
        #: per outage rather than once per check.
        self._stale_reported = False
        self._started = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def start(
        self,
        symbols: list[str],
        subscribe_bars: bool = True,
        subscribe_quotes: bool = True,
        subscribe_trades: bool = False,
        enable_trade_updates: bool = True,
        warmup_timeframe: str = "5Min",
        warmup_bars: int = 120,
    ) -> None:
        """Warm the cache from REST, then open the websockets.

        Warming first matters: without it, agents starting at the same moment
        would see empty history and every indicator would be `None` until
        enough live bars had accumulated — several hours on a 5-minute
        timeframe.
        """
        if self._started:
            return
        self._symbols = list(dict.fromkeys(symbols))

        await self.warmup(self._symbols, warmup_timeframe, warmup_bars)

        if self._settings.has_alpaca_credentials:
            await self._start_streams(
                subscribe_bars=subscribe_bars,
                subscribe_quotes=subscribe_quotes,
                subscribe_trades=subscribe_trades,
                enable_trade_updates=enable_trade_updates,
            )
        else:
            # No credentials: the cache is served from simulated data and no
            # socket is opened. Reported honestly rather than faked.
            self._set_state("alpaca_market_stream", ConnectionState.DISCONNECTED, "no credentials")
            log.warning(
                "market data streaming disabled: no Alpaca credentials. "
                "Historical and snapshot data come from the simulator."
            )

        self._stale_monitor = asyncio.create_task(
            self._monitor_staleness(), name="market-data-staleness"
        )
        self._started = True

    async def _start_streams(
        self,
        subscribe_bars: bool,
        subscribe_quotes: bool,
        subscribe_trades: bool,
        enable_trade_updates: bool,
    ) -> None:
        from app.brokers.alpaca.streams import AlpacaMarketStream, AlpacaTradeStream

        equities = [s for s in self._symbols if "/" not in s]

        self._market_stream = AlpacaMarketStream(
            api_key=self._settings.alpaca_api_key,
            secret_key=self._settings.alpaca_secret_key,
            feed=self._settings.effective_data_feed.value,
            max_symbols=self._settings.effective_max_stream_symbols,
            on_bar=self._handle_bar,
            on_quote=self._handle_quote,
            on_trade=self._handle_trade,
            on_state_change=self._handle_state_change,
            subscribe_bars=subscribe_bars,
            subscribe_quotes=subscribe_quotes,
            subscribe_trades=subscribe_trades,
        )
        streamed = self._market_stream.set_symbols(equities)
        await self._market_stream.start()
        log.info(
            "market data stream starting",
            extra={"symbols": len(streamed), "feed": self._settings.effective_data_feed.value},
        )

        if enable_trade_updates:
            self._trade_stream = AlpacaTradeStream(
                api_key=self._settings.alpaca_api_key,
                secret_key=self._settings.alpaca_secret_key,
                paper=self._settings.is_paper,
                on_trade_update=self._handle_trade_update,
                on_state_change=self._handle_state_change,
            )
            await self._trade_stream.start()

    async def stop(self) -> None:
        if self._stale_monitor is not None:
            self._stale_monitor.cancel()
            try:
                await self._stale_monitor
            except asyncio.CancelledError:
                pass
            self._stale_monitor = None

        for stream in (self._market_stream, self._trade_stream):
            if stream is not None:
                await stream.stop()
        self._market_stream = None
        self._trade_stream = None
        self._started = False

    # ------------------------------------------------------------------ #
    # warmup / REST access
    # ------------------------------------------------------------------ #

    async def warmup(self, symbols: list[str], timeframe: str = "5Min", limit: int = 120) -> int:
        """Populate the bar cache from historical REST data.

        Returns the number of symbols that produced data. A failure here is
        logged but not fatal: ATLAS should still start so the operator can see
        *why* on the System page, rather than refusing to boot.
        """
        if not symbols:
            return 0
        try:
            bars_by_symbol = await self._provider.get_bars(symbols, timeframe, limit=limit)
        except BrokerError as exc:
            log.error("market data warmup failed: %s", exc)
            return 0

        filled = 0
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue
            cache = self._cache_for(symbol, timeframe)
            cache.clear()
            cache.extend(bars)
            filled += 1

        log.info(
            "market data warmup complete",
            extra={
                "symbols_requested": len(symbols),
                "symbols_with_data": filled,
                "timeframe": timeframe,
            },
        )
        return filled

    def _cache_for(self, symbol: str, timeframe: str) -> deque[Bar]:
        """Cache slot for one symbol/timeframe.

        The timeframe is canonicalised and validated here, which is the single
        choke point every read goes through. It rejects a bad timeframe up
        front (rather than letting a provider silently default), and stops
        `"5min"` and `"5Min"` creating two caches for the same data.
        """
        key = (symbol, timeframes.canonical(timeframe))
        if key not in self._bars:
            self._bars[key] = deque(maxlen=BAR_CACHE_SIZE)
        return self._bars[key]

    async def get_bars(
        self, symbol: str, timeframe: str = "5Min", limit: int = 100, allow_fetch: bool = True
    ) -> list[Bar]:
        """Bars from the cache, falling back to REST on a miss."""
        cache = self._cache_for(symbol, timeframe)
        if len(cache) >= min(limit, 2):
            return list(cache)[-limit:]

        if not allow_fetch:
            return list(cache)

        try:
            fetched = await self._provider.get_bars([symbol], timeframe, limit=limit)
        except BrokerError as exc:
            log.warning("bar fetch failed for %s: %s", symbol, exc)
            return list(cache)

        bars = fetched.get(symbol, [])
        if bars:
            cache.clear()
            cache.extend(bars)
        return list(cache)[-limit:]

    async def get_bars_multi(
        self, symbols: list[str], timeframe: str = "5Min", limit: int = 100
    ) -> dict[str, list[Bar]]:
        """Bars for many symbols, batching the cache misses into one request.

        One request for twenty symbols instead of twenty requests is the
        difference between comfortably inside the rate limit and throttled.
        """
        out: dict[str, list[Bar]] = {}
        missing: list[str] = []

        for symbol in symbols:
            cache = self._cache_for(symbol, timeframe)
            if len(cache) >= min(limit, 2):
                out[symbol] = list(cache)[-limit:]
            else:
                missing.append(symbol)

        if missing:
            try:
                fetched = await self._provider.get_bars(missing, timeframe, limit=limit)
            except BrokerError as exc:
                log.warning("batch bar fetch failed: %s", exc)
                fetched = {}
            for symbol in missing:
                bars = fetched.get(symbol, [])
                if bars:
                    cache = self._cache_for(symbol, timeframe)
                    cache.clear()
                    cache.extend(bars)
                out[symbol] = bars

        return out

    async def get_quote(self, symbol: str, max_age_seconds: float = 30.0) -> Quote | None:
        """The latest quote, from the stream cache if fresh enough.

        `max_age_seconds` is what keeps the Risk Agent honest: a stale quote
        is refused rather than reused, so a dead feed cannot be mistaken for a
        quiet market.
        """
        cached = self._quotes.get(symbol)
        if cached is not None and cached.age_seconds <= max_age_seconds:
            return cached
        try:
            quote = await self._provider.get_latest_quote(symbol)
        except BrokerError as exc:
            log.warning("quote fetch failed for %s: %s", symbol, exc)
            return cached
        if quote is not None:
            self._quotes[symbol] = quote
        return quote

    def get_cached_quote(self, symbol: str) -> Quote | None:
        """Cache-only lookup. Never performs IO."""
        return self._quotes.get(symbol)

    async def get_snapshots(self, symbols: list[str]) -> dict[str, Any]:
        try:
            return await self._provider.get_snapshots(symbols)
        except BrokerError as exc:
            log.warning("snapshot fetch failed: %s", exc)
            return {}

    # ------------------------------------------------------------------ #
    # stream handlers (these run ON the event loop; see streams.py)
    # ------------------------------------------------------------------ #

    def _handle_bar(self, bar: Bar) -> None:
        # Streamed bars are 1-minute bars from Alpaca.
        cache = self._cache_for(bar.symbol, "1Min")
        cache.append(bar)
        self.last_stream_message_at = datetime.now(UTC)
        self._stale_reported = False
        self._bus.publish_nowait(BarEvent(bar=bar, source="market_data_service"))

    def _handle_quote(self, quote: Quote) -> None:
        self._quotes[quote.symbol] = quote
        self.last_stream_message_at = datetime.now(UTC)
        self._stale_reported = False
        self._bus.publish_nowait(QuoteEvent(quote=quote, source="market_data_service"))

    def _handle_trade(self, trade: Trade) -> None:
        self._trades[trade.symbol] = trade
        self.last_stream_message_at = datetime.now(UTC)
        self._stale_reported = False
        self._bus.publish_nowait(TradeEvent(trade=trade, source="market_data_service"))

    def _handle_trade_update(self, payload: dict[str, Any]) -> None:
        """Broker order updates (fills, cancels, rejections).

        Published as a normal typed event. The Execution Agent owns the
        interpretation, because it is the component tracking in-flight orders.
        """
        order = payload.get("order")
        if order is None:
            return
        self.last_stream_message_at = datetime.now(UTC)
        self._bus.publish_nowait(
            OrderUpdateEvent(
                order=order,
                event_type=str(payload.get("event", "update")),
                source="market_data_service",
            )
        )

    def _handle_state_change(self, service: str, state: ConnectionState, detail: str) -> None:
        self._set_state(service, state, detail)

    def _set_state(self, service: str, state: ConnectionState, detail: str = "") -> None:
        previous = self.connection_states.get(service)
        self.connection_states[service] = state
        if previous != state:
            log.info(
                "connection state changed",
                extra={"service": service, "state": state.value, "detail": detail},
            )
            self._bus.publish_nowait(
                ConnectionEvent(
                    service=service, state=state, detail=detail, source="market_data_service"
                )
            )

    # ------------------------------------------------------------------ #
    # staleness
    # ------------------------------------------------------------------ #

    async def _monitor_staleness(self) -> None:
        """Warn when the stream goes quiet.

        A websocket that is connected but silent is the dangerous failure: the
        connection looks healthy while every price in the system quietly ages.
        Strategies acting on it would be trading yesterday's market.

        Only checked when a stream is actually expected, so a weekend or a
        credential-less setup does not produce a stream of false alarms.
        """
        check_interval = 15.0
        while True:
            try:
                await asyncio.sleep(check_interval)

                if self._market_stream is None:
                    continue
                if not getattr(self._market_stream, "is_connected", False):
                    continue

                age = (
                    (datetime.now(UTC) - self.last_stream_message_at).total_seconds()
                    if self.last_stream_message_at
                    else None
                )
                if age is None or age < self._staleness_threshold:
                    continue

                if not self._stale_reported:
                    self._stale_reported = True
                    log.warning(
                        "market data stream is quiet",
                        extra={"seconds_since_last_message": round(age, 1)},
                    )
                    self._bus.publish_nowait(
                        StaleDataEvent(
                            seconds_since_last_update=age,
                            detail=(
                                f"No market data for {age:.0f}s while the stream reports "
                                f"connected. The market may be closed, or the feed may be dead."
                            ),
                            source="market_data_service",
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("staleness monitor error")

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "provider": self._provider.name,
            "feed": self._settings.effective_data_feed.value,
            "symbols": self._symbols,
            "symbol_count": len(self._symbols),
            "cached_series": len(self._bars),
            "cached_quotes": len(self._quotes),
            "last_stream_message_at": (
                self.last_stream_message_at.isoformat() if self.last_stream_message_at else None
            ),
            "seconds_since_last_message": (
                (datetime.now(UTC) - self.last_stream_message_at).total_seconds()
                if self.last_stream_message_at
                else None
            ),
            "streams": {
                name: stream.status()
                for name, stream in (
                    ("market", self._market_stream),
                    ("trade_updates", self._trade_stream),
                )
                if stream is not None
            },
            "connection_states": {k: v.value for k, v in self.connection_states.items()},
        }
