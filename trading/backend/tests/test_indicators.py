"""Indicator tests against known values.

These functions feed trading decisions, so they are checked against
hand-computed results and the canonical reference series rather than against
"whatever the code currently returns".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.market_data.indicators import (
    atr,
    average_volume,
    bollinger,
    compute_indicators,
    ema,
    ema_series,
    macd,
    momentum_pct,
    relative_volume,
    rsi,
    sma,
    stdev,
    swing_high,
    swing_low,
    true_range,
    volatility_pct,
    vwap,
)
from app.models.market import Bar
from tests.conftest import make_bars

#: Wilder's own worked RSI example. 15 closes, period 14.
WILDER_CLOSES = [
    44.34,
    44.09,
    44.15,
    43.61,
    44.33,
    44.83,
    45.10,
    45.42,
    45.84,
    46.08,
    45.89,
    46.03,
    45.61,
    46.28,
    46.28,
]


# --------------------------------------------------------------------------- #
# moving averages
# --------------------------------------------------------------------------- #


def test_sma_known_value() -> None:
    assert sma([1, 2, 3, 4, 5], 5) == 3.0
    assert sma([float(x) for x in range(1, 31)], 10) == 25.5


def test_sma_uses_only_the_last_n() -> None:
    assert sma([100, 100, 100, 1, 1], 2) == 1.0


def test_sma_insufficient_data_returns_none() -> None:
    assert sma([1, 2], 5) is None
    assert sma([], 1) is None


def test_ema_of_constant_series_is_the_constant() -> None:
    assert ema([7.0] * 50, 10) == pytest.approx(7.0)


def test_ema_series_is_seeded_with_sma() -> None:
    """Seeding with the SMA (not the first value) is the standard convention."""
    values = [float(x) for x in range(1, 21)]
    series = ema_series(values, 5)
    assert series[0] == pytest.approx(sum(values[:5]) / 5)
    assert len(series) == len(values) - 5 + 1


def test_ema_reacts_faster_than_sma() -> None:
    """The whole point of an EMA."""
    values = [10.0] * 20 + [20.0] * 5
    assert ema(values, 10) > sma(values, 10)


def test_ema_insufficient_data() -> None:
    assert ema([1, 2], 10) is None
    assert ema_series([1, 2], 10) == []


# --------------------------------------------------------------------------- #
# RSI
# --------------------------------------------------------------------------- #


def test_rsi_matches_wilder_reference() -> None:
    """The canonical reference value for this series is ~70.5."""
    assert rsi(WILDER_CLOSES, 14) == pytest.approx(70.46, abs=0.1)


def test_rsi_is_100_on_an_unbroken_rally() -> None:
    assert rsi([float(x) for x in range(1, 40)], 14) == 100.0


def test_rsi_is_zero_on_an_unbroken_decline() -> None:
    assert rsi([float(x) for x in range(40, 1, -1)], 14) == pytest.approx(0.0, abs=1e-9)


def test_rsi_of_flat_series_is_neutral() -> None:
    """No movement means no strength either way; must not divide by zero."""
    assert rsi([50.0] * 30, 14) == 50.0


def test_rsi_stays_in_range() -> None:
    import random

    rng = random.Random(7)
    values = [100.0]
    for _ in range(200):
        values.append(max(1.0, values[-1] * (1 + rng.gauss(0, 0.02))))
    result = rsi(values, 14)
    assert result is not None and 0.0 <= result <= 100.0


def test_rsi_needs_period_plus_one() -> None:
    assert rsi(WILDER_CLOSES[:14], 14) is None
    assert rsi(WILDER_CLOSES[:15], 14) is not None


# --------------------------------------------------------------------------- #
# MACD
# --------------------------------------------------------------------------- #


def test_macd_positive_in_an_uptrend() -> None:
    values = [100.0 + i for i in range(80)]
    macd_line, signal, histogram = macd(values)
    assert macd_line is not None and macd_line > 0
    assert signal is not None
    assert histogram == pytest.approx(macd_line - signal)


def test_macd_negative_in_a_downtrend() -> None:
    values = [200.0 - i for i in range(80)]
    macd_line, _, _ = macd(values)
    assert macd_line is not None and macd_line < 0


def test_macd_of_flat_series_is_zero() -> None:
    macd_line, signal, histogram = macd([50.0] * 80)
    assert macd_line == pytest.approx(0.0)
    assert signal == pytest.approx(0.0)
    assert histogram == pytest.approx(0.0)


def test_macd_insufficient_data() -> None:
    assert macd([1.0] * 10) == (None, None, None)


# --------------------------------------------------------------------------- #
# ATR and volatility
# --------------------------------------------------------------------------- #


def test_true_range_uses_the_widest_measure() -> None:
    bar = Bar(
        symbol="T",
        timestamp=datetime.now(UTC),
        open=100,
        high=105,
        low=99,
        close=104,
        volume=1,
    )
    # high-low = 6; |high - prev_close| = 15; |low - prev_close| = 9
    assert true_range(bar, previous_close=90.0) == 15.0
    assert true_range(bar, previous_close=104.0) == 6.0


def test_atr_of_constant_range_bars() -> None:
    """Bars with a constant 1.0 range and no gaps give ATR exactly 1.0."""
    bars = make_bars(count=50, start_price=100.0, trend=0.0, high_offset=0.5, low_offset=0.5)
    assert atr(bars, 14) == pytest.approx(1.0)


def test_atr_insufficient_data() -> None:
    assert atr(make_bars(count=5), 14) is None


def test_stdev_known_value() -> None:
    # Population stdev of 2,4,4,4,5,5,7,9 is exactly 2.
    assert stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.0)
    assert stdev([5.0]) is None


def test_bollinger_bands_bracket_the_mean() -> None:
    values = [float(x) for x in range(1, 41)]
    upper, middle, lower = bollinger(values, 20, 2.0)
    assert upper is not None and middle is not None and lower is not None
    assert lower < middle < upper
    assert middle == pytest.approx(sma(values, 20))


def test_bollinger_collapses_on_a_flat_series() -> None:
    upper, middle, lower = bollinger([10.0] * 30, 20, 2.0)
    assert upper == pytest.approx(10.0)
    assert middle == pytest.approx(10.0)
    assert lower == pytest.approx(10.0)


def test_volatility_of_flat_series_is_zero() -> None:
    assert volatility_pct([100.0] * 40, 20) == pytest.approx(0.0)


def test_volatility_rises_with_noise() -> None:
    import random

    rng = random.Random(3)
    calm = [100.0 * (1 + rng.gauss(0, 0.001)) for _ in range(60)]
    wild = [100.0 * (1 + rng.gauss(0, 0.05)) for _ in range(60)]
    assert volatility_pct(wild, 20) > volatility_pct(calm, 20)


# --------------------------------------------------------------------------- #
# VWAP
# --------------------------------------------------------------------------- #


def test_vwap_is_volume_weighted_not_a_simple_mean() -> None:
    """The heavier bar must pull VWAP toward its own price."""
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    bars = [
        Bar(symbol="T", timestamp=now, open=10, high=10, low=10, close=10, volume=100),
        Bar(
            symbol="T",
            timestamp=now + timedelta(minutes=5),
            open=20,
            high=20,
            low=20,
            close=20,
            volume=900,
        ),
    ]
    # (10*100 + 20*900) / 1000 = 19.0, not the simple mean of 15.
    assert vwap(bars) == pytest.approx(19.0)


def test_vwap_uses_only_the_latest_session() -> None:
    """VWAP is a session statistic; spanning days makes it meaningless."""
    day1 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    day2 = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)
    bars = [
        Bar(symbol="T", timestamp=day1, open=10, high=10, low=10, close=10, volume=1000),
        Bar(symbol="T", timestamp=day2, open=50, high=50, low=50, close=50, volume=1000),
    ]
    assert vwap(bars, session_only=True) == pytest.approx(50.0)
    assert vwap(bars, session_only=False) == pytest.approx(30.0)


def test_vwap_handles_zero_volume() -> None:
    now = datetime.now(UTC)
    bars = [Bar(symbol="T", timestamp=now, open=10, high=10, low=10, close=10, volume=0)]
    assert vwap(bars) is None


def test_vwap_of_empty_list() -> None:
    assert vwap([]) is None


# --------------------------------------------------------------------------- #
# volume
# --------------------------------------------------------------------------- #


def test_average_volume_excludes_the_forming_bar() -> None:
    """Including a partial bar understates the average and inflates RVOL."""
    bars = make_bars(count=21, volume=1_000_000.0)
    bars[-1] = bars[-1].model_copy(update={"volume": 1.0})
    assert average_volume(bars, period=20, exclude_current=True) == pytest.approx(1_000_000.0)
    assert average_volume(bars, period=21, exclude_current=False) < 1_000_000.0


def test_relative_volume_known_value() -> None:
    bars = make_bars(count=21, volume=1_000_000.0)
    bars[-1] = bars[-1].model_copy(update={"volume": 2_500_000.0})
    assert relative_volume(bars, period=20) == pytest.approx(2.5)


def test_relative_volume_insufficient_data() -> None:
    assert relative_volume(make_bars(count=1)) is None


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #


def test_swing_high_and_low() -> None:
    bars = make_bars(count=20, start_price=100.0, trend=1.0, high_offset=0.5, low_offset=0.5)
    assert swing_high(bars, 5) == pytest.approx(bars[-1].high)
    assert swing_low(bars, 5) == pytest.approx(bars[-5].low)


def test_swing_of_empty_list() -> None:
    assert swing_high([], 5) is None
    assert swing_low([], 5) is None


# --------------------------------------------------------------------------- #
# momentum
# --------------------------------------------------------------------------- #


def test_momentum_known_value() -> None:
    values = [100.0] * 10 + [110.0]
    assert momentum_pct(values, 10) == pytest.approx(10.0)


def test_momentum_negative_on_a_decline() -> None:
    values = [100.0] * 10 + [90.0]
    assert momentum_pct(values, 10) == pytest.approx(-10.0)


# --------------------------------------------------------------------------- #
# the aggregate
# --------------------------------------------------------------------------- #


def test_compute_indicators_on_an_uptrend(bars_uptrend: list[Bar]) -> None:
    result = compute_indicators("TEST", bars_uptrend, timeframe="5Min")
    assert result.bar_count == len(bars_uptrend)
    assert result.close == bars_uptrend[-1].close
    assert result.ema_fast is not None and result.ema_slow is not None
    assert result.ema_stack_bullish is True, "a rising series should stack bullish"
    assert result.rsi is not None and result.rsi > 50
    assert result.atr is not None and result.atr > 0
    assert result.momentum_pct is not None and result.momentum_pct > 0


def test_compute_indicators_missing_fields_are_none_not_guesses() -> None:
    """With too little history, an honest None beats a number from thin air."""
    result = compute_indicators("TEST", make_bars(count=5), timeframe="5Min")
    assert result.bar_count == 5
    assert result.close is not None
    assert result.ema_slow is None, "EMA50 cannot be computed from 5 bars"
    assert result.sma_long is None
    assert result.rsi is None
    assert result.ema_stack_bullish is None


def test_compute_indicators_on_empty_bars() -> None:
    result = compute_indicators("TEST", [], timeframe="5Min")
    assert result.bar_count == 0
    assert result.close is None
    assert result.ema_stack_bullish is None


def test_atr_percent_is_relative_to_price(bars_uptrend: list[Bar]) -> None:
    result = compute_indicators("TEST", bars_uptrend)
    assert result.atr is not None and result.atr_percent is not None
    assert result.atr_percent == pytest.approx(result.atr / result.close * 100)
