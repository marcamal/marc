"""Persistence: writes bus events to the database, and reads them back.

The recorder subscribes to the bus rather than being called by the agents.
That inversion matters: agents do not know the database exists, so persistence
can never slow down or break a trading decision. If a write fails, the event
is logged and dropped — losing a journal row is acceptable, stalling the
execution path is not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select

from app.core.logging import get_logger
from app.database.models import (
    AgentEventRow,
    JournalEntryRow,
    OrderRow,
    PortfolioSnapshotRow,
    RiskDecisionRow,
    RiskEventRow,
    ScannerResultRow,
    SignalRow,
    TradeProposalRow,
)
from app.database.session import Database
from app.events.bus import EventBus
from app.events.types import (
    AgentStatusEvent,
    Event,
    ExecutionSubmittedEvent,
    KillSwitchEvent,
    MentorExplanationEvent,
    OrderUpdateEvent,
    PortfolioSnapshotEvent,
    ProposalEvent,
    RiskDecisionEvent,
    RiskEvent,
    ScannerResultsEvent,
    SignalEvent,
    Topics,
)

log = get_logger(__name__)


class Recorder:
    """Subscribes to the bus and persists what matters."""

    #: Only these topics are persisted. Bars and quotes are deliberately
    #: absent: storing every tick would be gigabytes a week for no current
    #: benefit. See RETENTION_DAYS and the ROADMAP note on tick archiving.
    TOPICS = [
        Topics.PROPOSAL_CREATED,
        Topics.RISK_DECISION,
        Topics.RISK_EVENT,
        Topics.ORDER_UPDATE,
        Topics.EXECUTION_SUBMITTED,
        Topics.PORTFOLIO_SNAPSHOT,
        Topics.SCANNER_RESULTS,
        Topics.SIGNAL_ANY,
        Topics.AGENT_STATUS,
        Topics.KILL_SWITCH,
        Topics.MENTOR_EXPLANATION,
    ]

    def __init__(self, database: Database, bus: EventBus, trading_mode: str = "paper") -> None:
        self.db = database
        self.bus = bus
        self.trading_mode = trading_mode
        self.rows_written = 0
        self.write_errors = 0
        self._subscriptions: list[Any] = []
        #: Snapshots are written on a slower cadence than they are published,
        #: so the equity curve stays useful without a row every 30 seconds.
        self._last_snapshot_at: datetime | None = None
        self._snapshot_interval = timedelta(minutes=5)

    def start(self) -> None:
        # ONE subscription covering every topic, not one per topic. A
        # subscription owns a queue and a worker task, so separate
        # subscriptions would process events concurrently — and the same order
        # arrives on both `execution.submitted` and `order.update`, so two
        # workers would race to insert it and trip the unique constraint on
        # client_order_id. A single subscription serialises the writes.
        self._subscriptions.append(self.bus.subscribe(self.TOPICS, self._handle, name="recorder"))
        log.info("recorder subscribed", extra={"topics": len(self.TOPICS)})

    async def stop(self) -> None:
        for sub in self._subscriptions:
            await self.bus.unsubscribe(sub)
        self._subscriptions.clear()

    async def _handle(self, event: Event) -> None:
        """Route one event to its writer.

        Never raises: a persistence failure must not propagate back into the
        bus and from there into an agent.
        """
        try:
            await self._write(event)
        except Exception:
            self.write_errors += 1
            log.exception("failed to persist event", extra={"topic": event.topic})

    async def _write(self, event: Event) -> None:
        if isinstance(event, ProposalEvent):
            await self._write_proposal(event)
        elif isinstance(event, RiskDecisionEvent):
            await self._write_risk_decision(event)
        elif isinstance(event, RiskEvent):
            await self._write_risk_event(event)
        elif isinstance(event, (OrderUpdateEvent, ExecutionSubmittedEvent)):
            await self._write_order(event)
        elif isinstance(event, PortfolioSnapshotEvent):
            await self._write_snapshot(event)
        elif isinstance(event, ScannerResultsEvent):
            await self._write_scanner_results(event)
        elif isinstance(event, SignalEvent):
            await self._write_signal(event)
        elif isinstance(event, AgentStatusEvent):
            await self._write_agent_event(event)
        elif isinstance(event, KillSwitchEvent):
            await self._write_kill_switch(event)
        elif isinstance(event, MentorExplanationEvent):
            await self._write_mentor(event)

    # ------------------------------------------------------------------ #
    # writers
    # ------------------------------------------------------------------ #

    async def _write_proposal(self, event: ProposalEvent) -> None:
        p = event.proposal
        async with self.db.session() as session:
            await session.merge(
                TradeProposalRow(
                    id=p.id,
                    strategy_id=p.strategy_id,
                    symbol=p.symbol,
                    side=p.side.value,
                    quantity=p.quantity,
                    entry_price=p.entry_price,
                    stop_price=p.stop_price,
                    target_price=p.target_price,
                    order_type=p.order_type.value,
                    estimated_risk=p.estimated_risk,
                    confidence=p.confidence,
                    evidence=[e.model_dump(mode="json") for e in p.evidence],
                    invalidation=p.invalidation,
                    expires_at=p.expires_at,
                    created_at=p.created_at,
                    trace_id=p.trace_id,
                )
            )
        self.rows_written += 1

    async def _write_risk_decision(self, event: RiskDecisionEvent) -> None:
        d = event.decision
        async with self.db.session() as session:
            session.add(
                RiskDecisionRow(
                    proposal_id=d.proposal_id,
                    decision=d.decision.value,
                    approved_quantity=d.approved_quantity,
                    checks=[c.model_dump(mode="json") for c in d.checks],
                    reasons=d.reasons,
                    created_at=d.decided_at,
                    trace_id=d.trace_id,
                )
            )
        self.rows_written += 1

    async def _write_risk_event(self, event: RiskEvent) -> None:
        async with self.db.session() as session:
            session.add(
                RiskEventRow(
                    severity=event.severity,
                    rule=event.rule,
                    detail=event.detail,
                    observed=event.observed,
                    limit_value=event.limit,
                    created_at=event.timestamp,
                    trace_id=event.trace_id,
                )
            )
        self.rows_written += 1

    async def _write_order(self, event: OrderUpdateEvent | ExecutionSubmittedEvent) -> None:
        order = event.order
        if not order.id:
            return
        async with self.db.session() as session:
            # merge, not add: the same order arrives repeatedly as it moves
            # from accepted to partially filled to filled.
            await session.merge(
                OrderRow(
                    id=order.id,
                    client_order_id=order.client_order_id or order.id,
                    symbol=order.symbol,
                    side=order.side.value,
                    order_type=order.order_type.value,
                    time_in_force=order.time_in_force.value,
                    status=order.status.value,
                    quantity=order.quantity,
                    filled_quantity=order.filled_quantity,
                    limit_price=order.limit_price,
                    stop_price=order.stop_price,
                    average_fill_price=order.average_fill_price,
                    submitted_at=order.submitted_at,
                    filled_at=order.filled_at,
                    reason=order.reason,
                    proposal_id=order.proposal_id,
                    strategy_id=order.strategy_id,
                    trading_mode=self.trading_mode,
                    trace_id=order.trace_id,
                )
            )
        self.rows_written += 1

    async def _write_snapshot(self, event: PortfolioSnapshotEvent) -> None:
        now = datetime.now(UTC)
        if (
            self._last_snapshot_at is not None
            and now - self._last_snapshot_at < self._snapshot_interval
        ):
            return
        self._last_snapshot_at = now

        s = event.snapshot
        async with self.db.session() as session:
            session.add(
                PortfolioSnapshotRow(
                    equity=s.equity,
                    cash=s.cash,
                    buying_power=s.buying_power,
                    total_exposure=s.total_exposure,
                    total_exposure_pct=s.total_exposure_pct,
                    unrealized_pl=s.unrealized_pl,
                    realized_pl_today=s.realized_pl_today,
                    daily_pl=s.daily_pl,
                    daily_pl_pct=s.daily_pl_pct,
                    high_water_mark=s.high_water_mark,
                    drawdown_pct=s.drawdown_pct,
                    open_position_count=s.open_position_count,
                    exposure_by_group=s.exposure_by_correlation_group,
                    trading_mode=self.trading_mode,
                    created_at=s.as_of,
                )
            )
        self.rows_written += 1

    async def _write_scanner_results(self, event: ScannerResultsEvent) -> None:
        async with self.db.session() as session:
            for result in event.results:
                session.add(
                    ScannerResultRow(
                        symbol=result.symbol,
                        universe=event.universe,
                        rank=result.rank,
                        score=result.score,
                        metrics=result.model_dump(
                            mode="json",
                            include={
                                "price",
                                "percent_change",
                                "volume",
                                "relative_volume",
                                "atr_percent",
                                "rsi",
                                "momentum_pct",
                                "spread_percent",
                                "distance_from_vwap_pct",
                            },
                        ),
                        score_breakdown=result.score_breakdown,
                        notes=result.notes,
                        created_at=result.as_of,
                    )
                )
        self.rows_written += len(event.results)

    async def _write_signal(self, event: SignalEvent) -> None:
        s = event.signal
        async with self.db.session() as session:
            await session.merge(
                SignalRow(
                    id=s.id,
                    agent_id=s.agent_id,
                    symbol=s.symbol,
                    direction=s.direction.value,
                    confidence=s.confidence,
                    score=s.score,
                    timeframe=s.timeframe,
                    evidence=[e.model_dump(mode="json") for e in s.evidence],
                    risk_flags=s.risk_flags,
                    notes=s.notes,
                    created_at=s.created_at,
                    trace_id=s.trace_id,
                )
            )
        self.rows_written += 1

    async def _write_agent_event(self, event: AgentStatusEvent) -> None:
        async with self.db.session() as session:
            session.add(
                AgentEventRow(
                    agent_id=event.agent_id,
                    event_type="status_change",
                    status=event.status,
                    detail=event.current_task or event.detail,
                    created_at=event.timestamp,
                    trace_id=event.trace_id,
                )
            )
        self.rows_written += 1

    async def _write_kill_switch(self, event: KillSwitchEvent) -> None:
        async with self.db.session() as session:
            session.add(
                RiskEventRow(
                    severity="critical",
                    rule="kill_switch_engaged" if event.engaged else "kill_switch_released",
                    detail=(
                        f"{event.reason} (by {event.triggered_by}; "
                        f"{event.orders_canceled} orders cancelled, "
                        f"{event.positions_flattened} positions flattened)"
                    ),
                    created_at=event.timestamp,
                )
            )
        self.rows_written += 1

    async def _write_mentor(self, event: MentorExplanationEvent) -> None:
        async with self.db.session() as session:
            session.add(
                JournalEntryRow(
                    entry_type=f"mentor_{event.category}",
                    symbol=event.symbol,
                    title=event.title,
                    body=event.body,
                    lessons=event.lessons,
                    created_at=event.timestamp,
                    trace_id=event.trace_id,
                )
            )
        self.rows_written += 1

    def status(self) -> dict[str, Any]:
        return {
            "rows_written": self.rows_written,
            "write_errors": self.write_errors,
            "topics": len(self.TOPICS),
            "trading_mode": self.trading_mode,
        }


class Queries:
    """Read helpers for the API. Plain functions over the session."""

    def __init__(self, database: Database) -> None:
        self.db = database

    async def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(OrderRow).order_by(desc(OrderRow.created_at)).limit(limit)
            )
            return [_row_to_dict(row) for row in result.scalars().all()]

    async def recent_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(TradeProposalRow).order_by(desc(TradeProposalRow.created_at)).limit(limit)
            )
            return [_row_to_dict(row) for row in result.scalars().all()]

    async def recent_risk_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(RiskDecisionRow).order_by(desc(RiskDecisionRow.created_at)).limit(limit)
            )
            return [_row_to_dict(row) for row in result.scalars().all()]

    async def recent_risk_events(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(RiskEventRow).order_by(desc(RiskEventRow.created_at)).limit(limit)
            )
            return [_row_to_dict(row) for row in result.scalars().all()]

    async def equity_curve(self, limit: int = 500) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(PortfolioSnapshotRow)
                .order_by(desc(PortfolioSnapshotRow.created_at))
                .limit(limit)
            )
            rows = [_row_to_dict(row) for row in result.scalars().all()]
            return list(reversed(rows))  # oldest first, for charting

    async def journal(
        self, limit: int = 50, symbol: str | None = None, search: str | None = None
    ) -> list[dict[str, Any]]:
        """The searchable trading journal."""
        async with self.db.session() as session:
            query = select(JournalEntryRow).order_by(desc(JournalEntryRow.created_at))
            if symbol:
                query = query.where(JournalEntryRow.symbol == symbol.upper())
            if search:
                pattern = f"%{search}%"
                query = query.where(
                    JournalEntryRow.title.ilike(pattern) | JournalEntryRow.body.ilike(pattern)
                )
            result = await session.execute(query.limit(limit))
            return [_row_to_dict(row) for row in result.scalars().all()]

    async def decision_trace(self, trace_id: str) -> dict[str, Any]:
        """Every record sharing one trace id, in pipeline order.

        This is the query behind "WHY DID ATLAS BUY THIS?".
        """
        async with self.db.session() as session:
            out: dict[str, Any] = {"trace_id": trace_id}

            for key, model in (
                ("signals", SignalRow),
                ("proposals", TradeProposalRow),
                ("risk_decisions", RiskDecisionRow),
                ("orders", OrderRow),
                ("risk_events", RiskEventRow),
                ("agent_events", AgentEventRow),
                ("journal", JournalEntryRow),
            ):
                result = await session.execute(
                    select(model).where(model.trace_id == trace_id)  # type: ignore[attr-defined]
                )
                out[key] = [_row_to_dict(row) for row in result.scalars().all()]

            return out


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Serialise an ORM row, converting datetimes to ISO strings."""
    out: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        out[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return out
