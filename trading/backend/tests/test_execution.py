"""Execution tests, centred on duplicate-order prevention.

A retry that opens a second position is the most expensive bug an execution
system can have, so these tests simulate every way it could happen: a replayed
proposal, a broker that reports a duplicate id, and a connection that fails
*after* the broker accepted the order.
"""

from __future__ import annotations

import pytest

from app.agents.execution_agent import ExecutionAgent
from app.brokers.base import (
    BrokerConnectionError,
    DuplicateOrder,
    OrderRejected,
    OrderRequest,
)
from app.brokers.simulated import SimulatedBroker
from app.execution.tracker import OrderTracker, trading_day
from app.models.enums import OrderStatus, RiskDecisionType, Side
from app.models.trading import Order, RiskDecision, TradeProposal
from app.runtime import AtlasRuntime

# --------------------------------------------------------------------------- #
# the tracker: idempotency primitives
# --------------------------------------------------------------------------- #


def test_client_order_id_is_deterministic(valid_proposal: TradeProposal) -> None:
    """The same proposal and attempt must always produce the same id.

    This is what lets the broker recognise a retry as the same order.
    """
    tracker = OrderTracker()
    first = tracker.client_order_id_for(valid_proposal, attempt=0)
    second = tracker.client_order_id_for(valid_proposal, attempt=0)

    assert first == second
    assert first.startswith("atlas-")
    assert len(first) <= 48, "must stay well inside Alpaca's 128-char limit"


def test_client_order_id_differs_per_attempt(valid_proposal: TradeProposal) -> None:
    tracker = OrderTracker()
    assert tracker.client_order_id_for(valid_proposal, 0) != tracker.client_order_id_for(
        valid_proposal, 1
    )


def test_client_order_ids_differ_per_proposal() -> None:
    tracker = OrderTracker()
    a = TradeProposal(
        strategy_id="s",
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        entry_price=100,
        stop_price=99,
    )
    b = TradeProposal(
        strategy_id="s",
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        entry_price=100,
        stop_price=99,
    )
    assert tracker.client_order_id_for(a, 0) != tracker.client_order_id_for(b, 0)


def test_tracker_detects_an_already_submitted_proposal(
    valid_proposal: TradeProposal,
) -> None:
    tracker = OrderTracker()
    assert tracker.already_submitted(valid_proposal) is None

    order = Order(
        id="broker-1",
        client_order_id=tracker.client_order_id_for(valid_proposal, 0),
        symbol="SPY",
        side=Side.BUY,
        order_type="market",  # type: ignore[arg-type]
        time_in_force="day",  # type: ignore[arg-type]
        status=OrderStatus.FILLED,
        quantity=1,
    )
    tracker.record_submission(valid_proposal, order)

    assert tracker.already_submitted(valid_proposal) is order


def test_tracker_counts_orders(valid_proposal: TradeProposal) -> None:
    tracker = OrderTracker()
    assert tracker.orders_today == 0

    for i in range(3):
        proposal = valid_proposal.model_copy(update={"id": f"prop-{i}"})
        tracker.record_submission(
            proposal,
            Order(
                id=f"b{i}",
                client_order_id=f"atlas-{i}",
                symbol="SPY",
                side=Side.BUY,
                order_type="market",  # type: ignore[arg-type]
                time_in_force="day",  # type: ignore[arg-type]
                status=OrderStatus.FILLED,
                quantity=1,
            ),
        )

    assert tracker.orders_today == 3
    assert tracker.orders_today_by_symbol()["SPY"] == 3
    assert tracker.orders_today_for_strategy("test_strategy") == 3


def test_trading_day_follows_the_exchange_not_utc() -> None:
    """Counters must not reset in the middle of the US session."""
    from datetime import UTC, datetime

    # 02:00 UTC is still the previous US trading day.
    assert trading_day(datetime(2026, 3, 11, 2, 0, tzinfo=UTC)).day == 10
    # 18:00 UTC is mid-session on the same day.
    assert trading_day(datetime(2026, 3, 11, 18, 0, tzinfo=UTC)).day == 11


# --------------------------------------------------------------------------- #
# the simulated broker enforces unique client ids
# --------------------------------------------------------------------------- #


async def test_broker_rejects_a_reused_client_order_id(broker: SimulatedBroker) -> None:
    """Mirrors the real Alpaca behaviour the retry logic depends on."""
    request = OrderRequest(symbol="SPY", side="buy", quantity=1, client_order_id="atlas-dup-test")
    await broker.submit_order(request)

    with pytest.raises(DuplicateOrder):
        await broker.submit_order(request)


async def test_broker_rejects_non_positive_quantity(broker: SimulatedBroker) -> None:
    with pytest.raises(OrderRejected):
        await broker.submit_order(
            OrderRequest(symbol="SPY", side="buy", quantity=0, client_order_id="zero")
        )


async def test_broker_rejects_insufficient_buying_power() -> None:
    broker = SimulatedBroker(starting_equity=100.0)
    with pytest.raises(OrderRejected, match="buying power"):
        await broker.submit_order(
            OrderRequest(symbol="SPY", side="buy", quantity=100, client_order_id="toobig")
        )


# --------------------------------------------------------------------------- #
# the Execution Agent
# --------------------------------------------------------------------------- #


def _approval(proposal: TradeProposal, quantity: float | None = None) -> RiskDecision:
    return RiskDecision(
        proposal_id=proposal.id,
        decision=(
            RiskDecisionType.APPROVED if quantity is None else RiskDecisionType.APPROVED_REDUCED
        ),
        approved_quantity=quantity if quantity is not None else proposal.quantity,
    )


def _execution(runtime: AtlasRuntime) -> ExecutionAgent:
    agent = runtime.agents.get("execution")
    assert isinstance(agent, ExecutionAgent)
    return agent


async def test_approved_proposal_is_submitted(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    agent = _execution(runtime)
    order = await agent.execute(valid_proposal, _approval(valid_proposal))

    assert order is not None
    assert order.symbol == "SPY"
    assert order.quantity == 1
    assert agent.submitted_count == 1


async def test_rejected_proposal_is_not_submitted(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    agent = _execution(runtime)
    rejection = RiskDecision(
        proposal_id=valid_proposal.id,
        decision=RiskDecisionType.REJECTED,
        reasons=["test rejection"],
    )
    assert await agent.execute(valid_proposal, rejection) is None
    assert agent.submitted_count == 0


async def test_executing_the_same_proposal_twice_submits_once(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """The core duplicate-order guarantee."""
    agent = _execution(runtime)

    first = await agent.execute(valid_proposal, _approval(valid_proposal))
    second = await agent.execute(valid_proposal, _approval(valid_proposal))

    assert first is not None
    assert second is None, "a replayed proposal must not open a second position"
    assert agent.submitted_count == 1
    assert agent.duplicate_prevented_count == 1

    orders = await runtime.broker.get_orders(status="all")
    assert len(orders) == 1


async def test_reduced_size_is_honoured(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """Execution must use what risk approved, not what the strategy asked."""
    agent = _execution(runtime)
    proposal = valid_proposal.model_copy(update={"quantity": 10})

    order = await agent.execute(proposal, _approval(proposal, quantity=3))

    assert order is not None
    assert order.quantity == 3, "must execute the risk-approved size"


async def test_reduced_approval_without_a_quantity_is_refused(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    agent = _execution(runtime)
    broken = RiskDecision(
        proposal_id=valid_proposal.id,
        decision=RiskDecisionType.APPROVED_REDUCED,
        approved_quantity=None,
    )
    assert await agent.execute(valid_proposal, broken) is None


async def test_decision_for_another_proposal_is_refused(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """An approval must not be reusable for a different trade."""
    agent = _execution(runtime)
    other = RiskDecision(
        proposal_id="prop-someone-else",
        decision=RiskDecisionType.APPROVED,
        approved_quantity=1,
    )
    assert await agent.execute(valid_proposal, other) is None
    assert agent.submitted_count == 0


async def test_kill_switch_blocks_execution(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    agent = _execution(runtime)
    await runtime.engage_kill_switch("test")

    assert await agent.execute(valid_proposal, _approval(valid_proposal)) is None
    assert agent.submitted_count == 0


async def test_execution_fails_closed_without_the_risk_agent(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """No veto authority means no orders. Belt and braces with the manager's
    protection of the risk agent."""
    agent = _execution(runtime)
    await runtime.agents.stop_agent("risk", force=True)

    assert await agent.execute(valid_proposal, _approval(valid_proposal)) is None
    assert agent.submitted_count == 0


# --------------------------------------------------------------------------- #
# the ambiguous-failure case
# --------------------------------------------------------------------------- #


async def test_connection_failure_recovers_an_order_that_did_land(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """The dangerous case: the order succeeded but the response was lost.

    The agent must ask the broker by client_order_id and adopt the existing
    order rather than retrying into a second position.
    """
    agent = _execution(runtime)
    expected_id = runtime.order_tracker.client_order_id_for(valid_proposal, 0)

    landed = Order(
        id="broker-landed",
        client_order_id=expected_id,
        symbol="SPY",
        side=Side.BUY,
        order_type="market",  # type: ignore[arg-type]
        time_in_force="day",  # type: ignore[arg-type]
        status=OrderStatus.FILLED,
        quantity=1,
        filled_quantity=1,
        average_fill_price=585.0,
    )

    calls = {"submit": 0, "lookup": 0}

    async def failing_submit(_: OrderRequest) -> Order:
        calls["submit"] += 1
        raise BrokerConnectionError("connection reset after the broker accepted it")

    async def lookup(client_order_id: str) -> Order | None:
        calls["lookup"] += 1
        return landed if client_order_id == expected_id else None

    runtime.broker.submit_order = failing_submit  # type: ignore[method-assign]
    runtime.broker.get_order_by_client_id = lookup  # type: ignore[method-assign]

    order = await agent.execute(valid_proposal, _approval(valid_proposal))

    assert order is not None, "the landed order should have been adopted"
    assert order.id == "broker-landed"
    assert calls["submit"] == 1, "must not retry once the order is found"
    assert calls["lookup"] == 1


async def test_connection_failure_retries_when_nothing_landed(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """A genuine failure should be retried, with a fresh attempt id."""
    agent = _execution(runtime)
    agent.retry_backoff = 0.0  # keep the test fast
    attempts = {"n": 0}

    real_submit = runtime.broker.submit_order

    async def flaky(request: OrderRequest) -> Order:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise BrokerConnectionError("transient")
        return await real_submit(request)

    runtime.broker.submit_order = flaky  # type: ignore[method-assign]
    runtime.broker.get_order_by_client_id = lambda _cid: _none()  # type: ignore[assignment]

    order = await agent.execute(valid_proposal, _approval(valid_proposal))

    assert order is not None
    assert attempts["n"] == 2, "should have retried exactly once"
    assert agent.submitted_count == 1


async def _none() -> None:
    return None


async def test_duplicate_from_broker_adopts_the_existing_order(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """If the broker says the id already exists, an earlier attempt landed."""
    agent = _execution(runtime)
    expected_id = runtime.order_tracker.client_order_id_for(valid_proposal, 0)

    existing = Order(
        id="broker-existing",
        client_order_id=expected_id,
        symbol="SPY",
        side=Side.BUY,
        order_type="market",  # type: ignore[arg-type]
        time_in_force="day",  # type: ignore[arg-type]
        status=OrderStatus.FILLED,
        quantity=1,
    )

    async def duplicate_submit(_: OrderRequest) -> Order:
        raise DuplicateOrder("client_order_id already exists")

    async def lookup(_: str) -> Order | None:
        return existing

    runtime.broker.submit_order = duplicate_submit  # type: ignore[method-assign]
    runtime.broker.get_order_by_client_id = lookup  # type: ignore[method-assign]

    order = await agent.execute(valid_proposal, _approval(valid_proposal))

    assert order is not None and order.id == "broker-existing"
    assert agent.duplicate_prevented_count == 1


async def test_hard_rejection_is_not_retried(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """A malformed or impossible order would just be rejected again."""
    agent = _execution(runtime)
    calls = {"n": 0}

    async def rejecting(_: OrderRequest) -> Order:
        calls["n"] += 1
        raise OrderRejected("asset not tradable")

    runtime.broker.submit_order = rejecting  # type: ignore[method-assign]

    assert await agent.execute(valid_proposal, _approval(valid_proposal)) is None
    assert calls["n"] == 1, "a hard rejection must not be retried"
    assert agent.rejected_count == 1


async def test_repeated_rejections_engage_the_kill_switch(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """A systematic problem must stop the system, not loop forever."""
    agent = _execution(runtime)
    threshold = runtime.config.risk.kill_switch.repeated_rejection_threshold

    async def rejecting(_: OrderRequest) -> Order:
        raise OrderRejected("always fails")

    runtime.broker.submit_order = rejecting  # type: ignore[method-assign]

    for i in range(threshold):
        proposal = valid_proposal.model_copy(update={"id": f"prop-reject-{i}"})
        await agent.execute(proposal, _approval(proposal))

    assert runtime.kill_switch.is_engaged is True


# --------------------------------------------------------------------------- #
# full pipeline through the bus
# --------------------------------------------------------------------------- #


@pytest.mark.integration
async def test_pipeline_from_proposal_to_order(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """proposal.created -> risk -> execution -> broker, over the real bus."""
    import asyncio

    from app.events.types import ProposalEvent

    quote = await runtime.market_data.get_latest_quote("SPY")
    assert quote is not None
    proposal = valid_proposal.model_copy(
        update={
            "entry_price": quote.ask_price,
            "stop_price": round(quote.ask_price * 0.98, 2),
            "target_price": round(quote.ask_price * 1.05, 2),
        }
    )

    await runtime.bus.publish(ProposalEvent(proposal=proposal))
    await asyncio.sleep(0.5)

    orders = await runtime.broker.get_orders(status="all")
    assert len(orders) == 1, f"expected exactly one order, got {len(orders)}"
    assert orders[0].symbol == "SPY"

    risk_agent = runtime.agents.get("risk")
    assert risk_agent.approvals + risk_agent.reductions == 1  # type: ignore[attr-defined]


@pytest.mark.integration
async def test_duplicate_bus_events_produce_one_order(
    runtime: AtlasRuntime, valid_proposal: TradeProposal
) -> None:
    """A duplicated event on the bus must not duplicate the position."""
    import asyncio

    from app.events.types import ProposalEvent

    quote = await runtime.market_data.get_latest_quote("SPY")
    assert quote is not None
    proposal = valid_proposal.model_copy(
        update={
            "entry_price": quote.ask_price,
            "stop_price": round(quote.ask_price * 0.98, 2),
            "target_price": round(quote.ask_price * 1.05, 2),
        }
    )

    # The same proposal published three times, as a buggy publisher or a
    # retried handler would.
    for _ in range(3):
        await runtime.bus.publish(ProposalEvent(proposal=proposal))
    await asyncio.sleep(0.6)

    orders = await runtime.broker.get_orders(status="all")
    assert len(orders) == 1, (
        f"a replayed proposal opened {len(orders)} orders; idempotency is broken"
    )
