"""Event topics and payloads.

Topics are dotted strings. Subscribers may use a trailing `*` wildcard, e.g.
`market.*` catches `market.bar` and `market.quote`. Keeping topics as data
rather than classes means the same bus can later sit on Redis Streams or NATS
without changing any publisher.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.enums import ConnectionState
from app.models.market import Bar, MarketClock, Quote, ScannerResult, Trade
from app.models.trading import (
    AccountSnapshot,
    Order,
    PortfolioSnapshot,
    RiskDecision,
    Signal,
    TradeProposal,
)


class Topics:
    """Every topic ATLAS publishes on.

    Grouped by producer. The `*_ANY` wildcards are for subscribers.
    """

    # --- market data (produced by the Market Data Service) ----------------
    MARKET_ANY = "market.*"
    MARKET_BAR = "market.bar"
    MARKET_QUOTE = "market.quote"
    MARKET_TRADE = "market.trade"
    MARKET_CLOCK = "market.clock"
    MARKET_STALE = "market.stale"

    # --- connection health -------------------------------------------------
    CONNECTION_ANY = "connection.*"
    CONNECTION_CHANGED = "connection.changed"

    # --- research ----------------------------------------------------------
    SCANNER_RESULTS = "scanner.results"
    SIGNAL_ANY = "signal.*"
    SIGNAL_TECHNICAL = "signal.technical"
    SIGNAL_NEWS = "signal.news"
    SIGNAL_FUNDAMENTAL = "signal.fundamental"
    SIGNAL_SENTIMENT = "signal.sentiment"
    SIGNAL_MACRO = "signal.macro"

    # --- the trade pipeline ------------------------------------------------
    PROPOSAL_CREATED = "proposal.created"
    RISK_DECISION = "risk.decision"
    RISK_EVENT = "risk.event"
    EXECUTION_SUBMITTED = "execution.submitted"
    EXECUTION_REJECTED = "execution.rejected"
    ORDER_UPDATE = "order.update"

    # --- portfolio ---------------------------------------------------------
    ACCOUNT_UPDATE = "account.update"
    PORTFOLIO_SNAPSHOT = "portfolio.snapshot"

    # --- system ------------------------------------------------------------
    SYSTEM_ANY = "system.*"
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPING = "system.stopping"
    SYSTEM_ERROR = "system.error"
    KILL_SWITCH = "system.kill_switch"

    # --- agents ------------------------------------------------------------
    AGENT_ANY = "agent.*"
    AGENT_STATUS = "agent.status"
    AGENT_LOG = "agent.log"

    # --- mentor / teaching -------------------------------------------------
    MENTOR_EXPLANATION = "mentor.explanation"
    JOURNAL_ENTRY = "journal.entry"


class Event(BaseModel):
    """Base envelope.

    `trace_id` is what links a bar, a signal, a proposal, a risk decision and
    an order into one explainable chain.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    topic: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = "system"
    trace_id: str | None = None

    def matches(self, pattern: str) -> bool:
        """Does this event's topic match a subscription pattern?"""
        return topic_matches(self.topic, pattern)


def topic_matches(topic: str, pattern: str) -> bool:
    """Wildcard topic matching.

    `*` alone matches everything; `market.*` matches `market.bar`. An exact
    string matches itself. Deliberately simple — no multi-level globbing,
    because nothing in ATLAS needs it and ambiguity here would be a bug
    factory.
    """
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])
    return topic == pattern


# --------------------------------------------------------------------------- #
# market data events
# --------------------------------------------------------------------------- #


class BarEvent(Event):
    topic: Literal["market.bar"] = "market.bar"
    bar: Bar


class QuoteEvent(Event):
    topic: Literal["market.quote"] = "market.quote"
    quote: Quote


class TradeEvent(Event):
    topic: Literal["market.trade"] = "market.trade"
    trade: Trade


class ClockEvent(Event):
    topic: Literal["market.clock"] = "market.clock"
    clock: MarketClock


class StaleDataEvent(Event):
    topic: Literal["market.stale"] = "market.stale"
    symbol: str | None = None
    seconds_since_last_update: float
    detail: str = ""


class ConnectionEvent(Event):
    topic: Literal["connection.changed"] = "connection.changed"
    #: e.g. "alpaca_trading", "alpaca_market_stream", "alpaca_trade_stream"
    service: str
    state: ConnectionState
    detail: str = ""


# --------------------------------------------------------------------------- #
# research events
# --------------------------------------------------------------------------- #


class ScannerResultsEvent(Event):
    topic: Literal["scanner.results"] = "scanner.results"
    results: list[ScannerResult]
    universe: str
    scanned_count: int = 0


class SignalEvent(Event):
    topic: str = "signal.technical"
    signal: Signal


# --------------------------------------------------------------------------- #
# trade pipeline events
# --------------------------------------------------------------------------- #


class ProposalEvent(Event):
    topic: Literal["proposal.created"] = "proposal.created"
    proposal: TradeProposal


class RiskDecisionEvent(Event):
    topic: Literal["risk.decision"] = "risk.decision"
    decision: RiskDecision
    proposal: TradeProposal


class RiskEvent(Event):
    """A risk condition worth recording, independent of any one proposal."""

    topic: Literal["risk.event"] = "risk.event"
    severity: Literal["info", "warning", "critical"] = "warning"
    rule: str
    detail: str
    observed: float | None = None
    limit: float | None = None


class OrderUpdateEvent(Event):
    topic: Literal["order.update"] = "order.update"
    order: Order
    #: Alpaca's trade-update event name: new, fill, partial_fill, canceled...
    event_type: str = "update"


class ExecutionSubmittedEvent(Event):
    topic: Literal["execution.submitted"] = "execution.submitted"
    order: Order
    proposal_id: str


class ExecutionRejectedEvent(Event):
    topic: Literal["execution.rejected"] = "execution.rejected"
    proposal_id: str
    symbol: str
    reason: str
    stage: Literal["risk", "mode_guard", "kill_switch", "broker", "internal"] = "internal"


# --------------------------------------------------------------------------- #
# portfolio events
# --------------------------------------------------------------------------- #


class AccountUpdateEvent(Event):
    topic: Literal["account.update"] = "account.update"
    account: AccountSnapshot


class PortfolioSnapshotEvent(Event):
    topic: Literal["portfolio.snapshot"] = "portfolio.snapshot"
    snapshot: PortfolioSnapshot


# --------------------------------------------------------------------------- #
# system / agent events
# --------------------------------------------------------------------------- #


class SystemEvent(Event):
    topic: str = "system.started"
    detail: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class KillSwitchEvent(Event):
    topic: Literal["system.kill_switch"] = "system.kill_switch"
    engaged: bool
    reason: str
    triggered_by: str = "manual"
    orders_canceled: int = 0
    positions_flattened: int = 0


class AgentStatusEvent(Event):
    topic: Literal["agent.status"] = "agent.status"
    agent_id: str
    status: str
    current_task: str | None = None
    detail: str = ""


class MentorExplanationEvent(Event):
    """A teaching moment, aimed at the operator rather than the machine."""

    topic: Literal["mentor.explanation"] = "mentor.explanation"
    title: str
    body: str
    symbol: str | None = None
    #: "signal", "rejection", "fill", "trade_review", "risk"
    category: str = "signal"
    lessons: list[str] = Field(default_factory=list)
