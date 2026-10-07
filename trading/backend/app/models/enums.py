"""Enumerations shared across ATLAS.

These are ATLAS's own vocabulary, deliberately separate from the Alpaca SDK's
enums. The broker adapter translates between the two. That boundary is what
lets a second broker be added later without touching agent code.
"""

from __future__ import annotations

import enum


class Side(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for long exposure, -1 for short."""
        return 1 if self is Side.BUY else -1


class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class TimeInForce(str, enum.Enum):
    DAY = "day"
    GTC = "gtc"
    OPG = "opg"
    CLS = "cls"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, enum.Enum):
    """Order lifecycle, mirroring Alpaca's states."""

    PENDING_NEW = "pending_new"
    NEW = "new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    REPLACED = "replaced"
    PENDING_CANCEL = "pending_cancel"
    HELD = "held"
    DONE_FOR_DAY = "done_for_day"
    STOPPED = "stopped"
    SUSPENDED = "suspended"
    CALCULATED = "calculated"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        """True when no further state change is expected.

        Used by the Execution Agent to decide when it can stop tracking an
        order, and by the idempotency guard to decide whether a retry is safe.
        """
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
            OrderStatus.REPLACED,
            OrderStatus.STOPPED,
        )

    @property
    def is_open(self) -> bool:
        return not self.is_terminal


class AssetClass(str, enum.Enum):
    US_EQUITY = "us_equity"
    US_OPTION = "us_option"
    CRYPTO = "crypto"
    UNSUPPORTED = "unsupported"


class PositionSide(str, enum.Enum):
    LONG = "long"
    SHORT = "short"


class SignalDirection(str, enum.Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class AgentStatus(str, enum.Enum):
    """Agent lifecycle as shown on the Agents page."""

    CREATED = "created"
    STARTING = "starting"
    IDLE = "idle"
    WORKING = "working"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    UNHEALTHY = "unhealthy"

    @property
    def is_running(self) -> bool:
        return self in (AgentStatus.IDLE, AgentStatus.WORKING, AgentStatus.UNHEALTHY)


class RiskDecisionType(str, enum.Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    # Approved, but at a smaller quantity than requested, because a limit
    # would otherwise have been breached. Never silently applied — the
    # Execution Agent logs the downsize and the Mentor Agent explains it.
    APPROVED_REDUCED = "approved_reduced"


class ConnectionState(str, enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"
    ERROR = "error"


class MarketStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    PRE_MARKET = "pre_market"
    AFTER_HOURS = "after_hours"
    UNKNOWN = "unknown"
