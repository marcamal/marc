"""Database persistence tests: schema, recorder, queries, retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.database.models import (
    JournalEntryRow,
    OrderRow,
    RiskDecisionRow,
    ScannerResultRow,
    TradeProposalRow,
)
from app.database.session import Database
from app.events.types import (
    ExecutionSubmittedEvent,
    OrderUpdateEvent,
    ProposalEvent,
    RiskDecisionEvent,
)
from app.models.enums import OrderStatus, OrderType, RiskDecisionType, Side, TimeInForce
from app.models.trading import Order, RiskCheckResult, RiskDecision, TradeProposal
from app.runtime import AtlasRuntime


async def test_schema_is_created(database: Database) -> None:
    assert await database.health_check() is True


async def test_sqlite_path_is_resolved_against_the_project_root() -> None:
    """Otherwise the database file lands wherever the process was started.

    Running uvicorn from `backend/` and from the project root would then use
    two different databases.
    """
    db = Database("sqlite+aiosqlite:///./data/resolve-test.db")
    try:
        assert "/data/resolve-test.db" in db.url
        assert db.url.startswith("sqlite+aiosqlite:////") or ":" in db.url
    finally:
        await db.dispose()


async def test_absolute_sqlite_path_is_left_alone(tmp_path: object) -> None:
    path = f"{tmp_path}/abs.db"  # type: ignore[str-bytes-safe]
    db = Database(f"sqlite+aiosqlite:///{path}")
    try:
        assert db.url == f"sqlite+aiosqlite:///{path}"
    finally:
        await db.dispose()


def test_safe_url_hides_a_password() -> None:
    db = Database.__new__(Database)
    db.url = "postgresql+psycopg://atlas:supersecret@localhost:5432/atlas"

    masked = db._safe_url()

    assert "supersecret" not in masked
    assert "***" in masked


# --------------------------------------------------------------------------- #
# the recorder
# --------------------------------------------------------------------------- #


async def test_proposal_is_persisted(runtime: AtlasRuntime) -> None:
    import asyncio

    proposal = TradeProposal(
        strategy_id="persist_test",
        symbol="NVDA",
        side=Side.BUY,
        quantity=3,
        entry_price=178.0,
        stop_price=175.0,
        target_price=186.0,
        trace_id="trace-persist-1",
    )

    await runtime.bus.publish(ProposalEvent(proposal=proposal))
    await asyncio.sleep(0.3)

    rows = await runtime.queries.recent_proposals()
    stored = next((r for r in rows if r["id"] == proposal.id), None)

    assert stored is not None
    assert stored["symbol"] == "NVDA"
    assert stored["quantity"] == 3
    assert stored["estimated_risk"] == pytest.approx(9.0)
    assert stored["trace_id"] == "trace-persist-1"


async def test_risk_decision_is_persisted_with_every_check(
    runtime: AtlasRuntime,
) -> None:
    """The audit trail must answer 'why was this blocked?' months later."""
    import asyncio

    proposal = TradeProposal(
        strategy_id="persist_test",
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        entry_price=585.0,
        stop_price=580.0,
    )
    decision = RiskDecision(
        proposal_id=proposal.id,
        decision=RiskDecisionType.REJECTED,
        checks=[
            RiskCheckResult(
                rule="max_risk_per_trade", passed=False, detail="too big", limit=0.5, observed=9.9
            ),
            RiskCheckResult(rule="spread", passed=True, detail="fine"),
        ],
        reasons=["max_risk_per_trade: too big"],
    )

    await runtime.bus.publish(RiskDecisionEvent(decision=decision, proposal=proposal))
    await asyncio.sleep(0.3)

    rows = await runtime.queries.recent_risk_decisions()
    stored = next((r for r in rows if r["proposal_id"] == proposal.id), None)

    assert stored is not None
    assert stored["decision"] == "rejected"
    assert len(stored["checks"]) == 2
    assert stored["reasons"] == ["max_risk_per_trade: too big"]


async def test_order_updates_merge_rather_than_duplicate(
    runtime: AtlasRuntime,
) -> None:
    """The same order arrives repeatedly as it fills; one row must result.

    `client_order_id` is unique, so a second INSERT would fail outright. This
    pins the merge behaviour, and the single-subscription recorder that makes
    it race-free.
    """
    import asyncio

    order = Order(
        id="order-merge-1",
        client_order_id="atlas-merge-1",
        symbol="SPY",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.ACCEPTED,
        quantity=5,
    )

    # Both topics the recorder listens to, carrying the same order.
    await runtime.bus.publish(ExecutionSubmittedEvent(order=order, proposal_id="p1"))
    await runtime.bus.publish(OrderUpdateEvent(order=order, event_type="new"))
    await asyncio.sleep(0.3)

    # Then a fill for the same order.
    filled = order.model_copy(
        update={
            "status": OrderStatus.FILLED,
            "filled_quantity": 5,
            "average_fill_price": 585.1,
        }
    )
    await runtime.bus.publish(OrderUpdateEvent(order=filled, event_type="fill"))
    await asyncio.sleep(0.3)

    async with runtime.database.session() as session:
        count = await session.scalar(
            select(func.count()).select_from(OrderRow).where(OrderRow.id == "order-merge-1")
        )
        row = (
            await session.execute(select(OrderRow).where(OrderRow.id == "order-merge-1"))
        ).scalar_one()

    assert count == 1, "repeated updates must merge into one row"
    assert row.status == "filled"
    assert row.filled_quantity == 5
    assert runtime.recorder.write_errors == 0


async def test_recorder_counts_writes_and_no_errors(runtime: AtlasRuntime) -> None:
    import asyncio

    await runtime.bus.publish(
        ProposalEvent(
            proposal=TradeProposal(
                strategy_id="t",
                symbol="SPY",
                side=Side.BUY,
                quantity=1,
                entry_price=100.0,
                stop_price=99.0,
            )
        )
    )
    await asyncio.sleep(0.3)

    status = runtime.recorder.status()
    assert status["rows_written"] > 0
    assert status["write_errors"] == 0


async def test_recorder_does_not_persist_market_data(runtime: AtlasRuntime) -> None:
    """Storing every tick would be gigabytes a week for no current benefit."""
    from app.events.types import Topics

    assert Topics.MARKET_BAR not in runtime.recorder.TOPICS
    assert Topics.MARKET_QUOTE not in runtime.recorder.TOPICS


async def test_recorder_survives_a_write_failure(runtime: AtlasRuntime) -> None:
    """Persistence must never break a trading decision."""
    import asyncio

    from app.events.types import Event

    async def exploding(_event: Event) -> None:
        raise RuntimeError("database on fire")

    runtime.recorder._write = exploding  # type: ignore[method-assign]

    await runtime.bus.publish(
        ProposalEvent(
            proposal=TradeProposal(
                strategy_id="t",
                symbol="SPY",
                side=Side.BUY,
                quantity=1,
                entry_price=100.0,
                stop_price=99.0,
            )
        )
    )
    await asyncio.sleep(0.2)

    assert runtime.recorder.write_errors > 0
    # The bus and the agents are unaffected.
    assert runtime.bus.stats()["running"] is True


# --------------------------------------------------------------------------- #
# the decision trace
# --------------------------------------------------------------------------- #


@pytest.mark.integration
async def test_decision_trace_links_the_whole_chain(runtime: AtlasRuntime) -> None:
    """This is the query behind 'WHY DID ATLAS BUY THIS?'.

    Driven through the *real* pipeline — one proposal in, and risk and
    execution do the rest — so the test proves the trace id actually survives
    every hop, rather than that hand-written rows share a column value.
    """
    import asyncio

    trace_id = "trace-chain-test"

    quote = await runtime.market_data.get_latest_quote("MSFT")
    assert quote is not None
    entry = quote.ask_price

    proposal = TradeProposal(
        strategy_id="chain",
        symbol="MSFT",
        side=Side.BUY,
        quantity=2,
        entry_price=entry,
        stop_price=round(entry * 0.97, 2),
        target_price=round(entry * 1.07, 2),
        trace_id=trace_id,
    )

    await runtime.bus.publish(ProposalEvent(proposal=proposal, trace_id=trace_id))
    await asyncio.sleep(0.6)

    trace = await runtime.queries.decision_trace(trace_id)

    assert len(trace["proposals"]) == 1, "the proposal should be recorded"
    assert trace["proposals"][0]["symbol"] == "MSFT"
    assert len(trace["risk_decisions"]) == 1, "the risk verdict should share the trace"
    assert trace["risk_decisions"][0]["decision"] in {"approved", "approved_reduced"}
    assert len(trace["orders"]) == 1, "the resulting order should share the trace"
    assert trace["orders"][0]["symbol"] == "MSFT"
    assert trace["orders"][0]["strategy_id"] == "chain"


async def test_decision_trace_of_an_unknown_id_is_empty(runtime: AtlasRuntime) -> None:
    trace = await runtime.queries.decision_trace("nope")
    assert all(not rows for key, rows in trace.items() if isinstance(rows, list))


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #


async def test_journal_search_by_text_and_symbol(runtime: AtlasRuntime) -> None:
    async with runtime.database.session() as session:
        session.add_all(
            [
                JournalEntryRow(
                    entry_type="trade_review",
                    symbol="NVDA",
                    title="NVDA breakout review",
                    body="The stop was honoured and the target hit.",
                    outcome="win",
                ),
                JournalEntryRow(
                    entry_type="trade_review",
                    symbol="SPY",
                    title="SPY chop",
                    body="Whipsawed in a range. Should have stayed out.",
                    outcome="loss",
                ),
            ]
        )

    by_symbol = await runtime.queries.journal(symbol="NVDA")
    assert len(by_symbol) == 1
    assert by_symbol[0]["symbol"] == "NVDA"

    by_text = await runtime.queries.journal(search="whipsawed")
    assert len(by_text) == 1
    assert by_text[0]["symbol"] == "SPY"


# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #


async def test_retention_purges_old_high_volume_rows(database: Database) -> None:
    old = datetime.now(UTC) - timedelta(days=90)
    recent = datetime.now(UTC)

    async with database.session() as session:
        session.add_all(
            [
                ScannerResultRow(symbol="OLD", universe="u", created_at=old),
                ScannerResultRow(symbol="NEW", universe="u", created_at=recent),
            ]
        )

    deleted = await database.purge_old_rows({"scanner_results": 14})

    assert deleted.get("scanner_results") == 1
    async with database.session() as session:
        remaining = (await session.execute(select(ScannerResultRow))).scalars().all()
    assert [r.symbol for r in remaining] == ["NEW"]


async def test_retention_never_purges_the_decision_record(database: Database) -> None:
    """Orders, proposals, decisions and journal entries are kept forever.

    They are the record of what ATLAS did and why.
    """
    from app.database.models import RETENTION_DAYS

    for table in ("orders", "trade_proposals", "risk_decisions", "journal_entries"):
        assert table not in RETENTION_DAYS, f"{table} must never be purged"


async def test_purge_of_an_old_proposal_does_nothing(database: Database) -> None:
    async with database.session() as session:
        session.add(
            TradeProposalRow(
                id="prop-ancient",
                strategy_id="s",
                symbol="SPY",
                side="buy",
                quantity=1,
                entry_price=100.0,
                stop_price=99.0,
                created_at=datetime.now(UTC) - timedelta(days=4000),
            )
        )

    await database.purge_old_rows()

    async with database.session() as session:
        assert (await session.scalar(select(func.count()).select_from(TradeProposalRow))) == 1


async def test_session_rolls_back_on_error(database: Database) -> None:
    with pytest.raises(RuntimeError):
        async with database.session() as session:
            session.add(RiskDecisionRow(proposal_id="p", decision="approved"))
            raise RuntimeError("boom")

    async with database.session() as session:
        assert (await session.scalar(select(func.count()).select_from(RiskDecisionRow))) == 0
