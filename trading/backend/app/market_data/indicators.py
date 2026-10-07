"""Technical indicators, implemented from scratch in pure Python.

Why not TA-Lib or pandas-ta:

*   TA-Lib needs a C library compiled per platform, which is a genuine
    obstacle on Windows and a bad first experience for a new project.
*   These functions are the inputs to trading decisions, so being able to read
    and unit-test every line matters more than shaving microseconds.

Conventions that hold throughout this module:

*   Input lists are ordered **oldest first**, matching what the broker returns.
*   Every function returns `None` when there is not enough data for the
    requested period. A number computed from insufficient data is worse than
    no number, because it looks trustworthy.
*   RSI and ATR use **Wilder smoothing**, which is what charting platforms
    use. A simple moving average of gains gives visibly different values, and
    a strategy tuned against one will misbehave on the other.
"""

from __future__ import annotations

import math
from datetime import datetime

from app.models.market import Bar, IndicatorSet


def _closes(bars: list[Bar]) -> list[float]:
    return [b.close for b in bars]


# --------------------------------------------------------------------------- #
# moving averages
# --------------------------------------------------------------------------- #


def sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last `period` values."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    """Full EMA series, seeded with the SMA of the first `period` values.

    Returns a list shorter than the input by `period - 1`. Seeding with an SMA
    (rather than the first value) is the standard convention and converges
    much faster on short histories.

    The series — not just the final value — is needed because MACD is an EMA
    *of* an EMA.
    """
    if period <= 0 or len(values) < period:
        return []

    multiplier = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for value in values[period:]:
        out.append((value - out[-1]) * multiplier + out[-1])
    return out


def ema(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


# --------------------------------------------------------------------------- #
# momentum / oscillators
# --------------------------------------------------------------------------- #


def rsi(values: list[float], period: int = 14) -> float | None:
    """Relative Strength Index, Wilder smoothed. Range 0-100.

    Needs `period + 1` values: the first difference consumes one.
    """
    if period <= 0 or len(values) < period + 1:
        return None

    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]

    gains = [max(d, 0.0) for d in deltas[:period]]
    losses = [abs(min(d, 0.0)) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for delta in deltas[period:]:
        gain = max(delta, 0.0)
        loss = abs(min(delta, 0.0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        # No down moves in the window. Conventionally RSI = 100; guarding this
        # avoids a ZeroDivisionError on a straight-line rally.
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    """MACD line, signal line, histogram.

    The two EMAs start at different offsets, so the fast series is trimmed to
    align with the slow one before subtracting. Getting that alignment wrong
    is the classic MACD bug and produces a line that looks plausible but is
    shifted in time.
    """
    if len(values) < slow + signal:
        return None, None, None

    fast_series = ema_series(values, fast)
    slow_series = ema_series(values, slow)
    if not fast_series or not slow_series:
        return None, None, None

    offset = len(fast_series) - len(slow_series)
    aligned_fast = fast_series[offset:]
    macd_line = [f - s for f, s in zip(aligned_fast, slow_series, strict=True)]

    signal_series = ema_series(macd_line, signal)
    if not signal_series:
        return macd_line[-1], None, None

    macd_value = macd_line[-1]
    signal_value = signal_series[-1]
    return macd_value, signal_value, macd_value - signal_value


def momentum_pct(values: list[float], period: int = 10) -> float | None:
    """Percent change over `period` bars."""
    if period <= 0 or len(values) < period + 1:
        return None
    past = values[-(period + 1)]
    if past == 0:
        return None
    return ((values[-1] - past) / past) * 100


# --------------------------------------------------------------------------- #
# volatility
# --------------------------------------------------------------------------- #


def true_range(bar: Bar, previous_close: float) -> float:
    return max(
        bar.high - bar.low,
        abs(bar.high - previous_close),
        abs(bar.low - previous_close),
    )


def atr(bars: list[Bar], period: int = 14) -> float | None:
    """Average True Range, Wilder smoothed.

    ATR is the backbone of position sizing here: stop distance in ATR units is
    far more stable across symbols than a fixed percentage.
    """
    if period <= 0 or len(bars) < period + 1:
        return None

    ranges = [true_range(bars[i], bars[i - 1].close) for i in range(1, len(bars))]
    value = sum(ranges[:period]) / period
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def stdev(values: list[float]) -> float | None:
    """Population standard deviation."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def bollinger(
    values: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[float | None, float | None, float | None]:
    """Upper, middle, lower band."""
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    middle = sum(window) / period
    deviation = stdev(window)
    if deviation is None:
        return None, middle, None
    return middle + num_std * deviation, middle, middle - num_std * deviation


def volatility_pct(values: list[float], period: int = 20) -> float | None:
    """Standard deviation of bar-to-bar percent returns.

    Reported per-bar, not annualised: the comparison that matters to the
    scanner is between symbols on the same timeframe.
    """
    if len(values) < period + 1:
        return None
    window = values[-(period + 1) :]
    returns = [
        ((window[i] - window[i - 1]) / window[i - 1]) * 100
        for i in range(1, len(window))
        if window[i - 1] != 0
    ]
    return stdev(returns)


# --------------------------------------------------------------------------- #
# volume / VWAP
# --------------------------------------------------------------------------- #


def vwap(bars: list[Bar], session_only: bool = True) -> float | None:
    """Volume-weighted average price using the typical price (H+L+C)/3.

    VWAP is a *session* statistic. Running it across several days produces a
    meaningless number that drifts further from the real intraday VWAP with
    every day included — so by default only bars from the most recent calendar
    date in the series are used.
    """
    if not bars:
        return None

    selected = bars
    if session_only:
        last_date = bars[-1].timestamp.date()
        selected = [b for b in bars if b.timestamp.date() == last_date]
        if not selected:
            selected = bars

    total_volume = sum(b.volume for b in selected)
    if total_volume <= 0:
        return None

    weighted = sum(((b.high + b.low + b.close) / 3) * b.volume for b in selected)
    return weighted / total_volume


def average_volume(bars: list[Bar], period: int = 20, exclude_current: bool = True) -> float | None:
    """Mean volume over `period` bars.

    `exclude_current` leaves out the most recent bar, which is usually still
    forming. Including a partial bar understates the average and inflates
    relative volume — the metric the scanner ranks on.
    """
    series = bars[:-1] if exclude_current and len(bars) > 1 else bars
    if len(series) < period:
        if not series:
            return None
        period = len(series)
    volumes = [b.volume for b in series[-period:]]
    return sum(volumes) / len(volumes)


def relative_volume(bars: list[Bar], period: int = 20) -> float | None:
    """Current bar volume divided by the recent average. 2.0 = twice normal."""
    if len(bars) < 2:
        return None
    baseline = average_volume(bars, period=period, exclude_current=True)
    if not baseline:
        return None
    return bars[-1].volume / baseline


# --------------------------------------------------------------------------- #
# price structure
# --------------------------------------------------------------------------- #


def swing_high(bars: list[Bar], lookback: int = 10) -> float | None:
    if not bars:
        return None
    return max(b.high for b in bars[-lookback:])


def swing_low(bars: list[Bar], lookback: int = 10) -> float | None:
    if not bars:
        return None
    return min(b.low for b in bars[-lookback:])


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #


def compute_indicators(
    symbol: str,
    bars: list[Bar],
    timeframe: str = "5Min",
    ema_fast: int = 20,
    ema_slow: int = 50,
    sma_long: int = 200,
    rsi_period: int = 14,
    atr_period: int = 14,
    bollinger_period: int = 20,
    bollinger_std: float = 2.0,
    momentum_period: int = 10,
    swing_lookback: int = 10,
    as_of: datetime | None = None,
) -> IndicatorSet:
    """Compute every indicator for one symbol on one timeframe.

    Short histories are fine: each field is independently `None` when its
    period is not satisfied, so a symbol with 30 bars still gets a usable
    EMA20 and RSI even though EMA50 and SMA200 are unavailable.
    """
    result = IndicatorSet(symbol=symbol, timeframe=timeframe, bar_count=len(bars))
    if as_of is not None:
        result.as_of = as_of
    if not bars:
        return result

    closes = _closes(bars)
    last_close = closes[-1]

    result.close = last_close
    result.ema_fast = ema(closes, ema_fast)
    result.ema_slow = ema(closes, ema_slow)
    result.sma_long = sma(closes, sma_long)
    result.rsi = rsi(closes, rsi_period)

    macd_value, macd_signal, macd_hist = macd(closes)
    result.macd = macd_value
    result.macd_signal = macd_signal
    result.macd_histogram = macd_hist

    result.atr = atr(bars, atr_period)
    if result.atr is not None and last_close > 0:
        result.atr_percent = (result.atr / last_close) * 100

    upper, middle, lower = bollinger(closes, bollinger_period, bollinger_std)
    result.bollinger_upper = upper
    result.bollinger_middle = middle
    result.bollinger_lower = lower

    result.vwap = vwap(bars)
    if result.vwap and result.vwap > 0:
        result.distance_from_vwap_pct = ((last_close - result.vwap) / result.vwap) * 100

    result.volume = bars[-1].volume
    result.average_volume = average_volume(bars, period=bollinger_period)
    result.relative_volume = relative_volume(bars, period=bollinger_period)
    result.momentum_pct = momentum_pct(closes, momentum_period)
    result.volatility_pct = volatility_pct(closes, bollinger_period)
    result.swing_high = swing_high(bars, swing_lookback)
    result.swing_low = swing_low(bars, swing_lookback)

    return result
