"""Timeframe parsing tests.

These exist because the definition used to be duplicated: the Alpaca adapter
rejected an invalid timeframe while the simulator silently fell back to
5-minute bars, so a typo worked offline and failed against the real broker.
The regression test at the bottom pins that down.
"""

from __future__ import annotations

import pytest

from app.market_data import timeframes
from app.market_data.timeframes import InvalidTimeframe


@pytest.mark.parametrize(
    ("value", "amount", "unit"),
    [
        ("1Min", 1, "min"),
        ("5Min", 5, "min"),
        ("15min", 15, "min"),
        ("30 Min", 30, "min"),
        ("1Minute", 1, "min"),
        ("1Hour", 1, "hour"),
        ("4hr", 4, "hour"),
        ("1Day", 1, "day"),
        ("Day", 1, "day"),
        ("1Week", 1, "week"),
        ("1Month", 1, "month"),
    ],
)
def test_parse(value: str, amount: int, unit: str) -> None:
    assert timeframes.parse(value) == (amount, unit)


@pytest.mark.parametrize(
    "value", ["", "banana", "0Min", "-5Min", "1Fortnight", "5", "Min5", "1 d a y"]
)
def test_parse_rejects_nonsense(value: str) -> None:
    with pytest.raises(InvalidTimeframe):
        timeframes.parse(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1Min", 1),
        ("5Min", 5),
        ("15Min", 15),
        ("1Hour", 60),
        ("4Hour", 240),
        ("1Day", 1440),
        ("1Week", 10080),
    ],
)
def test_minutes(value: str, expected: int) -> None:
    assert timeframes.minutes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5min", "5Min"),
        ("5Min", "5Min"),
        ("15 MINUTES", "15Min"),
        ("1hr", "1Hour"),
        ("day", "1Day"),
    ],
)
def test_canonical(value: str, expected: str) -> None:
    """Canonicalisation stops '5min' and '5Min' creating two caches."""
    assert timeframes.canonical(value) == expected


def test_is_valid() -> None:
    assert timeframes.is_valid("5Min") is True
    assert timeframes.is_valid("banana") is False


# --------------------------------------------------------------------------- #
# the regression this module exists to prevent
# --------------------------------------------------------------------------- #


async def test_simulator_rejects_what_alpaca_rejects() -> None:
    """The simulator must not be more permissive than the real adapter.

    It used to default to 5-minute bars for an unrecognised timeframe, so
    `timeframe=banana` returned data offline and errored in production.
    """
    from app.brokers.alpaca.market_data import parse_timeframe
    from app.brokers.simulated import SimulatedMarketData

    with pytest.raises(ValueError):
        parse_timeframe("banana")

    with pytest.raises(ValueError):
        await SimulatedMarketData().get_bars(["SPY"], "banana", limit=10)


async def test_service_rejects_an_invalid_timeframe(runtime: object) -> None:
    """The market data service validates before touching a provider."""
    service = runtime.data_service  # type: ignore[attr-defined]

    with pytest.raises(ValueError):
        await service.get_bars("SPY", timeframe="banana", limit=10)


async def test_service_treats_equivalent_spellings_as_one_series(
    runtime: object,
) -> None:
    service = runtime.data_service  # type: ignore[attr-defined]

    first = await service.get_bars("SPY", timeframe="5Min", limit=30)
    second = await service.get_bars("SPY", timeframe="5min", limit=30)

    assert [b.close for b in first] == [b.close for b in second]
