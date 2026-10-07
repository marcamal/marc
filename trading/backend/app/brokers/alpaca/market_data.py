"""Alpaca Market Data API adapter (REST).

Streaming lives in `streams.py`; this module is for historical bars and latest
snapshots.

Two Alpaca realities shape this file:

1.  **Stocks and crypto use different clients and different endpoints.**
    Crypto symbols contain a slash (`BTC/USD`) and go to the crypto client,
    which needs no data subscription. Symbols are routed automatically.

2.  **The free plan withholds the most recent 15 minutes of SIP data.** With
    `feed=iex` (the default) that restriction does not apply, which is why
    ATLAS defaults to IEX rather than SIP. See `Settings.effective_data_feed`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestQuoteRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from app.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    MarketDataProvider,
)
from app.core.logging import get_logger
from app.market_data import timeframes
from app.models.market import Bar, Quote, Snapshot, Trade

log = get_logger(__name__)

#: Canonical unit name (from app.market_data.timeframes) -> Alpaca's enum.
_UNIT_MAP = {
    "min": TimeFrameUnit.Minute,
    "hour": TimeFrameUnit.Hour,
    "day": TimeFrameUnit.Day,
    "week": TimeFrameUnit.Week,
    "month": TimeFrameUnit.Month,
}


def parse_timeframe(value: str) -> TimeFrame:
    """Turn `"5Min"`, `"1Hour"`, `"1Day"` into an Alpaca `TimeFrame`.

    Validation lives in `app.market_data.timeframes` so that the simulator and
    the real adapter accept exactly the same set of timeframes.
    """
    amount, unit = timeframes.parse(value)
    return TimeFrame(amount=amount, unit=_UNIT_MAP[unit])


def is_crypto_symbol(symbol: str) -> bool:
    """Alpaca crypto pairs are written `BASE/QUOTE`, e.g. `BTC/USD`."""
    return "/" in symbol


class AlpacaMarketData(MarketDataProvider):
    """Historical bars and latest quotes/trades/snapshots."""

    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, feed: str = "iex") -> None:
        if not api_key or not secret_key:
            raise BrokerAuthError(
                "Alpaca market data requires ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )
        self._feed = feed
        self._stock = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        # The crypto client takes the same keys but needs no data subscription.
        self._crypto = CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        log.info("Alpaca market data client created", extra={"feed": feed})

    @property
    def feed(self) -> str:
        return self._feed

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    async def _call(self, fn: Any, *args: Any, context: str, **kwargs: Any) -> Any:
        """Run a synchronous SDK method off the event loop."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except APIError as exc:
            status = getattr(exc, "status_code", None)
            message = str(exc)
            if status in (401, 403):
                raise BrokerAuthError(
                    f"{context}: market data access denied (HTTP {status}). "
                    f"If you requested the 'sip' feed, it needs the paid Algo Trader "
                    f"Plus plan. {message}"
                ) from exc
            if status == 429:
                raise BrokerConnectionError(
                    f"{context}: market data rate limit hit. {message}"
                ) from exc
            # The free plan returns an error for SIP data inside the last
            # 15 minutes. Say so explicitly instead of surfacing a raw 400.
            if "subscription" in message.lower() or "recent" in message.lower():
                raise BrokerError(
                    f"{context}: your market data plan does not cover this request. "
                    f"The free plan cannot read SIP data from the last 15 minutes; "
                    f"use ATLAS_STOCK_DATA_FEED=iex. {message}"
                ) from exc
            raise BrokerError(f"{context}: {message}") from exc
        except Exception as exc:
            raise BrokerError(f"{context}: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _bar_from_sdk(symbol: str, raw: Any) -> Bar:
        return Bar(
            symbol=symbol,
            timestamp=_aware(getattr(raw, "timestamp", None)) or datetime.now(UTC),
            open=float(getattr(raw, "open", 0) or 0),
            high=float(getattr(raw, "high", 0) or 0),
            low=float(getattr(raw, "low", 0) or 0),
            close=float(getattr(raw, "close", 0) or 0),
            volume=float(getattr(raw, "volume", 0) or 0),
            trade_count=(
                int(getattr(raw, "trade_count", 0) or 0)
                if getattr(raw, "trade_count", None) is not None
                else None
            ),
            vwap=(
                float(getattr(raw, "vwap", 0) or 0)
                if getattr(raw, "vwap", None) is not None
                else None
            ),
        )

    @staticmethod
    def _quote_from_sdk(symbol: str, raw: Any) -> Quote:
        return Quote(
            symbol=symbol,
            timestamp=_aware(getattr(raw, "timestamp", None)) or datetime.now(UTC),
            bid_price=float(getattr(raw, "bid_price", 0) or 0),
            bid_size=float(getattr(raw, "bid_size", 0) or 0),
            ask_price=float(getattr(raw, "ask_price", 0) or 0),
            ask_size=float(getattr(raw, "ask_size", 0) or 0),
        )

    @staticmethod
    def _trade_from_sdk(symbol: str, raw: Any) -> Trade:
        return Trade(
            symbol=symbol,
            timestamp=_aware(getattr(raw, "timestamp", None)) or datetime.now(UTC),
            price=float(getattr(raw, "price", 0) or 0),
            size=float(getattr(raw, "size", 0) or 0),
            exchange=str(getattr(raw, "exchange", "") or "") or None,
        )

    @staticmethod
    def _extract(response: Any, symbols: list[str]) -> dict[str, Any]:
        """Normalise an SDK response into a plain `{symbol: payload}` dict.

        `BarSet` and friends behave like mappings but are not dicts, and a
        single-symbol request can return the payload directly.
        """
        data = getattr(response, "data", None)
        if isinstance(data, dict):
            return data
        if isinstance(response, dict):
            return response
        if len(symbols) == 1 and response is not None:
            return {symbols[0]: response}
        return {}

    # ------------------------------------------------------------------ #
    # bars
    # ------------------------------------------------------------------ #

    async def get_bars(
        self,
        symbols: list[str],
        timeframe: str = "5Min",
        limit: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, list[Bar]]:
        """Historical bars per symbol, oldest first.

        Stock and crypto symbols may be mixed freely; they are split and
        fetched from the right endpoint, then merged.
        """
        if not symbols:
            return {}

        stocks = [s for s in symbols if not is_crypto_symbol(s)]
        cryptos = [s for s in symbols if is_crypto_symbol(s)]
        tf = parse_timeframe(timeframe)
        out: dict[str, list[Bar]] = {}

        if stocks:
            request = StockBarsRequest(
                symbol_or_symbols=stocks,
                timeframe=tf,
                limit=limit,
                start=start,
                end=end,
                feed=self._feed,
            )
            response = await self._call(
                self._stock.get_stock_bars, request, context="get_stock_bars"
            )
            for symbol, bars in self._extract(response, stocks).items():
                out[symbol] = [self._bar_from_sdk(symbol, b) for b in bars or []]

        if cryptos:
            request = CryptoBarsRequest(
                symbol_or_symbols=cryptos, timeframe=tf, limit=limit, start=start, end=end
            )
            response = await self._call(
                self._crypto.get_crypto_bars, request, context="get_crypto_bars"
            )
            for symbol, bars in self._extract(response, cryptos).items():
                out[symbol] = [self._bar_from_sdk(symbol, b) for b in bars or []]

        # Guarantee a key for every requested symbol so callers never KeyError
        # on a symbol that simply had no data.
        for symbol in symbols:
            out.setdefault(symbol, [])
        return out

    # ------------------------------------------------------------------ #
    # latest quote / trade
    # ------------------------------------------------------------------ #

    async def get_latest_quote(self, symbol: str) -> Quote | None:
        quotes = await self.get_latest_quotes([symbol])
        return quotes.get(symbol)

    async def get_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}

        stocks = [s for s in symbols if not is_crypto_symbol(s)]
        cryptos = [s for s in symbols if is_crypto_symbol(s)]
        out: dict[str, Quote] = {}

        if stocks:
            request = StockLatestQuoteRequest(symbol_or_symbols=stocks, feed=self._feed)
            response = await self._call(
                self._stock.get_stock_latest_quote, request, context="get_stock_latest_quote"
            )
            for symbol, quote in self._extract(response, stocks).items():
                if quote is not None:
                    out[symbol] = self._quote_from_sdk(symbol, quote)

        if cryptos:
            request = CryptoLatestQuoteRequest(symbol_or_symbols=cryptos)
            response = await self._call(
                self._crypto.get_crypto_latest_quote, request, context="get_crypto_latest_quote"
            )
            for symbol, quote in self._extract(response, cryptos).items():
                if quote is not None:
                    out[symbol] = self._quote_from_sdk(symbol, quote)

        return out

    async def get_latest_trade(self, symbol: str) -> Trade | None:
        if is_crypto_symbol(symbol):
            request = CryptoLatestTradeRequest(symbol_or_symbols=[symbol])
            response = await self._call(
                self._crypto.get_crypto_latest_trade, request, context="get_crypto_latest_trade"
            )
        else:
            request = StockLatestTradeRequest(symbol_or_symbols=[symbol], feed=self._feed)
            response = await self._call(
                self._stock.get_stock_latest_trade, request, context="get_stock_latest_trade"
            )
        raw = self._extract(response, [symbol]).get(symbol)
        return self._trade_from_sdk(symbol, raw) if raw is not None else None

    # ------------------------------------------------------------------ #
    # snapshots
    # ------------------------------------------------------------------ #

    async def get_snapshots(self, symbols: list[str]) -> dict[str, Snapshot]:
        """One call per batch giving trade + quote + minute/daily bars.

        This is the most efficient way to feed the scanner: one request covers
        price, volume, spread and the previous close for every symbol.
        """
        if not symbols:
            return {}

        stocks = [s for s in symbols if not is_crypto_symbol(s)]
        out: dict[str, Snapshot] = {}

        if stocks:
            request = StockSnapshotRequest(symbol_or_symbols=stocks, feed=self._feed)
            response = await self._call(
                self._stock.get_stock_snapshot, request, context="get_stock_snapshot"
            )
            for symbol, snap in self._extract(response, stocks).items():
                if snap is None:
                    continue
                out[symbol] = Snapshot(
                    symbol=symbol,
                    latest_trade=(
                        self._trade_from_sdk(symbol, snap.latest_trade)
                        if getattr(snap, "latest_trade", None)
                        else None
                    ),
                    latest_quote=(
                        self._quote_from_sdk(symbol, snap.latest_quote)
                        if getattr(snap, "latest_quote", None)
                        else None
                    ),
                    minute_bar=(
                        self._bar_from_sdk(symbol, snap.minute_bar)
                        if getattr(snap, "minute_bar", None)
                        else None
                    ),
                    daily_bar=(
                        self._bar_from_sdk(symbol, snap.daily_bar)
                        if getattr(snap, "daily_bar", None)
                        else None
                    ),
                    previous_daily_bar=(
                        self._bar_from_sdk(symbol, snap.previous_daily_bar)
                        if getattr(snap, "previous_daily_bar", None)
                        else None
                    ),
                )

        # Crypto has no snapshot endpoint shaped like the stock one, so build
        # an equivalent from the latest quote and trade.
        cryptos = [s for s in symbols if is_crypto_symbol(s)]
        if cryptos:
            quotes = await self.get_latest_quotes(cryptos)
            for symbol in cryptos:
                out[symbol] = Snapshot(symbol=symbol, latest_quote=quotes.get(symbol))

        return out


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None
