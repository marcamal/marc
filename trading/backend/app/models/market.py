"""Normalised market data models.

Agents consume these, never raw Alpaca SDK objects. The broker adapter is the
only place allowed to know what an `alpaca.data.models.Bar` looks like.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import MarketStatus


class Bar(BaseModel):
    """One OHLCV candle."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def range(self) -> float:
        return self.high - self.low

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_green(self) -> bool:
        return self.close >= self.open


class Quote(BaseModel):
    """Best bid and offer."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mid(self) -> float:
        """Midpoint, falling back to whichever side is populated.

        A one-sided book is normal pre-market and on thin IEX quotes, so this
        must not divide by zero or return a nonsense midpoint of half the ask.
        """
        if self.bid_price > 0 and self.ask_price > 0:
            return (self.bid_price + self.ask_price) / 2
        return self.ask_price or self.bid_price

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread(self) -> float:
        if self.bid_price > 0 and self.ask_price > 0:
            return self.ask_price - self.bid_price
        return 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_pct(self) -> float:
        """Spread as a percentage of the midpoint.

        Returns 0.0 for a one-sided book rather than infinity; the Risk Agent
        treats a missing quote as a separate failure (stale data) so that a
        thin book and an absent book are not conflated.
        """
        mid = self.mid
        if mid <= 0 or self.spread <= 0:
            return 0.0
        return (self.spread / mid) * 100

    @property
    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.timestamp).total_seconds()


class Trade(BaseModel):
    """A single executed print."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime
    price: float
    size: float
    exchange: str | None = None


class Snapshot(BaseModel):
    """Everything about one symbol at one moment."""

    symbol: str
    latest_trade: Trade | None = None
    latest_quote: Quote | None = None
    minute_bar: Bar | None = None
    daily_bar: Bar | None = None
    previous_daily_bar: Bar | None = None

    @property
    def price(self) -> float | None:
        """The best available current price.

        Preference order: last trade, quote midpoint, minute-bar close. On the
        free IEX feed a symbol can easily have a stale trade but a live quote,
        so the fallback chain matters in practice.
        """
        if self.latest_trade:
            return self.latest_trade.price
        if self.latest_quote:
            return self.latest_quote.mid
        if self.minute_bar:
            return self.minute_bar.close
        return None

    @property
    def percent_change_today(self) -> float | None:
        """Change versus the previous session's close."""
        price = self.price
        if price is None or not self.previous_daily_bar:
            return None
        prev_close = self.previous_daily_bar.close
        if prev_close <= 0:
            return None
        return ((price - prev_close) / prev_close) * 100


class MarketClock(BaseModel):
    """Exchange session state, straight from the broker.

    We never infer this from the local clock: holidays, half days and the
    operator's own timezone make that unreliable, and a wrong answer here
    means trading into a closed market.
    """

    timestamp: datetime
    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None

    @property
    def status(self) -> MarketStatus:
        return MarketStatus.OPEN if self.is_open else MarketStatus.CLOSED

    @property
    def seconds_until_close(self) -> float | None:
        if not self.is_open or self.next_close is None:
            return None
        return (self.next_close - self.timestamp).total_seconds()

    @property
    def seconds_until_open(self) -> float | None:
        if self.is_open or self.next_open is None:
            return None
        return (self.next_open - self.timestamp).total_seconds()


class IndicatorSet(BaseModel):
    """Indicator values for one symbol on one timeframe.

    Every field is optional: with fewer bars than an indicator's period, the
    honest answer is `None`, not a number computed from insufficient data.
    """

    symbol: str
    timeframe: str
    as_of: datetime = Field(default_factory=lambda: datetime.now(UTC))
    bar_count: int = 0

    close: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    sma_long: float | None = None
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    atr: float | None = None
    atr_percent: float | None = None
    bollinger_upper: float | None = None
    bollinger_middle: float | None = None
    bollinger_lower: float | None = None
    vwap: float | None = None
    distance_from_vwap_pct: float | None = None
    volume: float | None = None
    average_volume: float | None = None
    relative_volume: float | None = None
    momentum_pct: float | None = None
    volatility_pct: float | None = None
    swing_high: float | None = None
    swing_low: float | None = None

    @property
    def ema_stack_bullish(self) -> bool | None:
        """price > fast EMA > slow EMA — the simplest trend-alignment test."""
        if None in (self.close, self.ema_fast, self.ema_slow):
            return None
        return self.close > self.ema_fast > self.ema_slow  # type: ignore[operator]

    @property
    def above_vwap(self) -> bool | None:
        if self.close is None or self.vwap is None:
            return None
        return self.close > self.vwap


class ScannerResult(BaseModel):
    """One ranked scanner candidate.

    A scanner result is an *observation*, never an instruction. Nothing in
    ATLAS trades directly off this object.
    """

    symbol: str
    rank: int = 0
    score: float = 0.0
    as_of: datetime = Field(default_factory=lambda: datetime.now(UTC))

    price: float | None = None
    percent_change: float | None = None
    volume: float | None = None
    relative_volume: float | None = None
    atr: float | None = None
    atr_percent: float | None = None
    volatility_pct: float | None = None
    spread_percent: float | None = None
    distance_from_vwap_pct: float | None = None
    rsi: float | None = None
    momentum_pct: float | None = None
    ema_stack_bullish: bool | None = None

    #: Per-metric contributions to `score`, so the dashboard can show *why*
    #: a symbol ranked where it did instead of an unexplained number.
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    #: Short human-readable notes, e.g. "relative volume 2.1x".
    notes: list[str] = Field(default_factory=list)
    #: Filters this symbol failed, when it was excluded.
    failed_filters: list[str] = Field(default_factory=list)
