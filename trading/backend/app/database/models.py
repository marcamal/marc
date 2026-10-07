"""SQLAlchemy models.

Design notes:

*   **SQLite by default, PostgreSQL-ready.** The only SQLite-specific thing
    here is the URL in `.env`. Types are chosen to behave identically on both:
    `String` with explicit lengths, `Float` for money (see the caveat below),
    timezone-aware `DateTime`.

*   **Money is `Float`, not `Numeric`.** For a personal paper-trading journal
    this is fine — these are records of what a broker reported, not ledger
    entries that must balance to the cent. If ATLAS ever grows a tax ledger
    that must reconcile exactly, those columns should become `Numeric(18, 8)`;
    that is flagged in `ROADMAP.md` rather than solved prematurely.

*   **No tick storage in the MVP.** `MarketEventRow` exists for bars and
    notable events, deliberately not every quote. Storing every tick for 30
    symbols would be gigabytes a week to no benefit yet. `purge_old_rows()`
    implements the retention policy.

*   **Trace ids everywhere.** Every row that is part of a decision carries
    `trace_id`, so the whole chain behind a trade can be reconstructed with
    one indexed query.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Shared declarative base."""

    type_annotation_map = {dict: JSON, list: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )


# --------------------------------------------------------------------------- #
# market data
# --------------------------------------------------------------------------- #


class CandleRow(Base):
    """A stored OHLCV bar.

    Only bars ATLAS actually used for a decision, or explicitly requested for
    backtesting, are persisted. Not a general market-data archive.
    """

    __tablename__ = "candles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    vwap: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        # Covers "bars for symbol X on timeframe Y between two dates", which
        # is every query the backtester will make.
        Index("ix_candles_symbol_tf_ts", "symbol", "timeframe", "timestamp", unique=True),
    )


class MarketEventRow(Base, TimestampMixin):
    """A notable market occurrence: a gap, a halt, unusual volume, stale data."""

    __tablename__ = "market_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


# --------------------------------------------------------------------------- #
# research
# --------------------------------------------------------------------------- #


class SignalRow(Base, TimestampMixin):
    """An agent's structured opinion about a symbol."""

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    timeframe: Mapped[str] = mapped_column(String(16), default="5Min")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    risk_flags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class ScannerResultRow(Base, TimestampMixin):
    """One ranked scanner candidate, kept so rankings can be reviewed later."""

    __tablename__ = "scanner_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    universe: Mapped[str] = mapped_column(String(64), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[list] = mapped_column(JSON, default=list)


class ResearchDocumentRow(Base, TimestampMixin):
    """A news article, filing or AI research note. Always with its source."""

    __tablename__ = "research_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Mandatory for anything externally sourced: a claim without a URL cannot
    #: be verified later, and un-verifiable "news" is worse than none.
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    document_type: Mapped[str] = mapped_column(String(64), default="news")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# the trade pipeline
# --------------------------------------------------------------------------- #


class TradeProposalRow(Base, TimestampMixin):
    """A strategy's request to trade, stored whether or not it was approved.

    Rejected proposals are the more interesting half of the dataset: they are
    how you find out that a strategy keeps generating trades the risk rules
    will never allow.
    """

    __tablename__ = "trade_proposals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_price: Mapped[float] = mapped_column(Float, nullable=False)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    estimated_risk: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    invalidation: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class RiskDecisionRow(Base, TimestampMixin):
    """The Risk Agent's verdict, with every individual check.

    This is the audit trail. It answers "why was this blocked?" months later.
    """

    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    approved_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class OrderRow(Base, TimestampMixin):
    """An order as the broker reported it."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(8), default="day")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Which mode produced this order. Keeps paper and live history separable
    #: in the same database, which matters when reviewing performance.
    trading_mode: Mapped[str] = mapped_column(String(16), default="paper", index=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class PositionRow(Base, TimestampMixin):
    """A position snapshot, written on each reconciliation."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    side: Mapped[str] = mapped_column(String(8), default="long")
    average_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pl: Mapped[float] = mapped_column(Float, default=0.0)
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --------------------------------------------------------------------------- #
# portfolio and risk
# --------------------------------------------------------------------------- #


class PortfolioSnapshotRow(Base, TimestampMixin):
    """Periodic equity curve point. The basis of every performance metric."""

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    buying_power: Mapped[float] = mapped_column(Float, default=0.0)
    total_exposure: Mapped[float] = mapped_column(Float, default=0.0)
    total_exposure_pct: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pl_today: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    high_water_mark: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    open_position_count: Mapped[int] = mapped_column(Integer, default=0)
    exposure_by_group: Mapped[dict] = mapped_column(JSON, default=dict)
    trading_mode: Mapped[str] = mapped_column(String(16), default="paper", index=True)


class RiskEventRow(Base, TimestampMixin):
    """A risk condition worth recording: a breach, a kill switch, a mismatch."""

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning", index=True)
    rule: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    observed: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


# --------------------------------------------------------------------------- #
# agents, strategies, journal
# --------------------------------------------------------------------------- #


class AgentEventRow(Base, TimestampMixin):
    """Agent lifecycle and notable actions."""

    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class StrategyRunRow(Base, TimestampMixin):
    """One evaluation pass of one strategy."""

    __tablename__ = "strategy_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbols_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    proposals_generated: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BacktestRow(Base, TimestampMixin):
    """A completed backtest and its metrics. Populated in Phase 2."""

    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbols: Mapped[list] = mapped_column(JSON, default=list)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")


class JournalEntryRow(Base, TimestampMixin):
    """A trade review or a lesson. The searchable trading journal."""

    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_type: Mapped[str] = mapped_column(String(32), default="trade_review", index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    #: The structured review from MentorAgent.build_trade_review().
    review: Mapped[dict] = mapped_column(JSON, default=dict)
    lessons: Mapped[list] = mapped_column(JSON, default=list)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    profit_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


#: Retention policy: table -> days to keep.
#: Decisions, orders and journal entries are kept indefinitely — they are the
#: record of what ATLAS did and why. High-volume, low-value rows expire.
RETENTION_DAYS: dict[str, int] = {
    "candles": 90,
    "market_events": 30,
    "signals": 60,
    "scanner_results": 14,
    "agent_events": 14,
    "strategy_runs": 30,
    "positions": 30,
}
