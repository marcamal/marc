"""The single definition of a valid timeframe.

This module exists because the definition was previously duplicated: the
Alpaca adapter validated and rejected `"banana"`, while the simulator quietly
fell back to 5-minute bars. A typo therefore *worked* in offline development
and failed against the real broker — the worst kind of divergence, because
tests would pass.

Both providers and the market data service now resolve timeframes here, so a
timeframe is either valid everywhere or rejected everywhere.
"""

from __future__ import annotations

import re

_PATTERN = re.compile(r"^(\d*)\s*(min|minute|hour|hr|day|week|month)s?$", re.IGNORECASE)

#: Canonical unit name -> minutes in one unit.
_UNIT_MINUTES = {
    "min": 1,
    "hour": 60,
    "day": 1440,
    "week": 10080,
    "month": 43200,  # nominal 30 days; only used for ordering and simulation
}

#: Input unit spelling -> canonical unit name.
_UNIT_ALIASES = {
    "min": "min",
    "minute": "min",
    "hour": "hour",
    "hr": "hour",
    "day": "day",
    "week": "week",
    "month": "month",
}

#: Canonical unit -> the spelling Alpaca's TimeFrameUnit uses.
ALPACA_UNIT_NAMES = {
    "min": "Min",
    "hour": "Hour",
    "day": "Day",
    "week": "Week",
    "month": "Month",
}


class InvalidTimeframe(ValueError):
    """Raised for an unrecognised timeframe string.

    Deliberately an error rather than a silent default: analysing 1-minute
    bars while believing they are daily is a bug that produces confident,
    wrong answers.
    """


def parse(value: str) -> tuple[int, str]:
    """Split a timeframe into `(amount, canonical_unit)`.

    `"5Min"` -> `(5, "min")`, `"1Day"` -> `(1, "day")`, `"Day"` -> `(1, "day")`.

    Raises:
        InvalidTimeframe: on anything unrecognised.
    """
    match = _PATTERN.match(value.strip())
    if not match:
        raise InvalidTimeframe(
            f"Unrecognised timeframe {value!r}. Use forms like '1Min', '5Min', "
            f"'15Min', '1Hour', '4Hour', '1Day', '1Week'."
        )

    amount_text, unit_text = match.groups()
    amount = int(amount_text) if amount_text else 1
    if amount < 1:
        raise InvalidTimeframe(f"Timeframe amount must be >= 1, got {value!r}")

    return amount, _UNIT_ALIASES[unit_text.lower()]


def minutes(value: str) -> int:
    """Minutes covered by one bar of this timeframe."""
    amount, unit = parse(value)
    return amount * _UNIT_MINUTES[unit]


def canonical(value: str) -> str:
    """Normalise to the spelling ATLAS uses internally, e.g. `"5Min"`.

    Used as a cache key so that `"5min"` and `"5Min"` do not produce two
    separate series for the same data.
    """
    amount, unit = parse(value)
    return f"{amount}{ALPACA_UNIT_NAMES[unit]}"


def is_valid(value: str) -> bool:
    try:
        parse(value)
    except InvalidTimeframe:
        return False
    return True
