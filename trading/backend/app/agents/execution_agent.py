"""Execution Agent — the ONLY component that may submit orders.

Nothing else in ATLAS calls `broker.submit_order`. Strategies produce
proposals; the Risk Agent approves them; this agent, and only this agent,
turns an approval into an order.

Its defences, in the order they apply:

1.  **Mode guard.** BACKTEST and REPLAY never reach a broker. LIVE requires
    all three gates in `Settings.live_gates`.
2.  **Risk Agent presence.** If the Risk Agent is not running, every proposal
    is refused. Fail closed — an execution path with no veto authority is
    strictly worse than no execution path.
3.  **Decision binding.** Only a proposal carrying a current approval is
    acted on, and an `APPROVED_REDUCED` decision is executed at the *reduced*
    size, never the requested one.
4.  **Idempotency.** A deterministic `client_order_id` per attempt, plus a
    post-failure lookup by that id, so a network retry can never double a
    position. This is the defence that matters most: see
    `app.execution.tracker`.
5.  **Rejection circuit breaker.** Consecutive broker rejections engage the
    kill switch rather than retrying forever.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    DuplicateOrder,
    OrderRejected,
    OrderRequest,
)
from app.config.settings import TradingMode
from app.events.types import (
    ExecutionRejectedEvent,
    ExecutionSubmittedEvent,
    OrderUpdateEvent,
    RiskDecisionEvent,
    Topics,
)
from app.models.enums import RiskDecisionType
from app.models.trading import Order, RiskDecision, TradeProposal

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime


class ExecutionAgent(Agent):
    """Submits approved orders and tracks their lifecycle."""

    agent_type = "execution"
    inputs = [Topics.RISK_DECISION, Topics.ORDER_UPDATE]
    outputs = [Topics.EXECUTION_SUBMITTED, Topics.EXECUTION_REJECTED, Topics.ORDER_UPDATE]
    tools = ["broker.submit_order", "broker.get_order_by_client_id", "broker.cancel_order"]
    subscriptions = [Topics.RISK_DECISION, Topics.ORDER_UPDATE]

    def __init__(self, runtime: AtlasRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        self.max_retries = int(self.config.get("max_submit_retries", 3))
        self.retry_backoff = float(self.config.get("retry_backoff_seconds", 2))

        self.submitted_count = 0
        self.rejected_count = 0
        self.duplicate_prevented_count = 0

    # ------------------------------------------------------------------ #
    # event handling
    # ------------------------------------------------------------------ #

    async def handle_event(self, event: Any) -> None:
        if isinstance(event, RiskDecisionEvent):
            if event.decision.is_approved:
                await self.execute(event.proposal, event.decision)
            else:
                # The Risk Agent already logged the veto; record it on the
                # execution timeline too so the pipeline reads end to end.
                await self.publish(
                    ExecutionRejectedEvent(
                        proposal_id=event.proposal.id,
                        symbol=event.proposal.symbol,
                        reason=event.decision.summary(),
                        stage="risk",
                        source=self.id,
                    )
                )
            return

        if isinstance(event, OrderUpdateEvent):
            self.runtime.order_tracker.update_order(event.order)
            self.info(
                f"order update: {event.order.symbol} {event.event_type} "
                f"status={event.order.status.value} "
                f"filled={event.order.filled_quantity:g}/{event.order.quantity:g}"
            )

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #

    async def execute(self, proposal: TradeProposal, decision: RiskDecision) -> Order | None:
        """Submit an approved proposal. Returns the order, or None if refused."""
        self.set_task(f"executing {proposal.symbol}")

        guard_failure = await self._guard(proposal, decision)
        if guard_failure is not None:
            return None

        quantity = proposal.quantity
        if decision.decision is RiskDecisionType.APPROVED_REDUCED:
            if decision.approved_quantity is None or decision.approved_quantity <= 0:
                await self._refuse(
                    proposal, "risk approved a reduced size but gave no quantity", "internal"
                )
                return None
            # Execute what risk approved, never what the strategy asked for.
            quantity = decision.approved_quantity
            self.info(
                f"executing reduced size for {proposal.symbol}: "
                f"{proposal.quantity:g} -> {quantity:g}"
            )

        return await self._submit_with_retries(proposal, quantity)

    async def _guard(self, proposal: TradeProposal, decision: RiskDecision) -> str | None:
        """Pre-submission checks independent of the Risk Agent's verdict.

        Returns a reason string if refused, None if clear.
        """
        settings = self.runtime.settings
        mode = settings.effective_mode

        # --- 1. mode guard -----------------------------------------------
        if mode in (TradingMode.BACKTEST, TradingMode.REPLAY):
            await self._refuse(
                proposal, f"mode {mode.value} never submits broker orders", "mode_guard"
            )
            return "mode"

        if mode is TradingMode.LIVE:
            closed = [name for name, open_ in settings.live_gates.items() if not open_]
            if closed:
                # Should be unreachable: effective_mode only returns LIVE when
                # every gate is open. Checked again because this is the last
                # point before real money moves.
                await self._refuse(
                    proposal,
                    f"LIVE mode requested but gates are closed: {closed}",
                    "mode_guard",
                )
                return "live_gate"

        # --- 2. kill switch ----------------------------------------------
        if self.runtime.kill_switch.is_engaged:
            await self._refuse(
                proposal,
                f"kill switch engaged: {self.runtime.kill_switch.reason}",
                "kill_switch",
            )
            return "kill_switch"

        # --- 3. the Risk Agent must be alive -----------------------------
        if not self.runtime.agents.is_running("risk"):
            await self._refuse(
                proposal,
                "Risk Agent is not running. Execution fails closed: no veto "
                "authority means no orders.",
                "mode_guard",
            )
            return "no_risk_agent"

        # --- 4. the decision must belong to this proposal ----------------
        if decision.proposal_id != proposal.id:
            await self._refuse(
                proposal,
                f"risk decision is for proposal {decision.proposal_id}, not {proposal.id}",
                "internal",
            )
            return "decision_mismatch"

        if not decision.is_approved:
            await self._refuse(proposal, decision.summary(), "risk")
            return "not_approved"

        # --- 5. idempotency: has this proposal already been executed? ----
        existing = self.runtime.order_tracker.already_submitted(proposal)
        if existing is not None:
            self.duplicate_prevented_count += 1
            self.warn(
                f"duplicate execution prevented for {proposal.symbol}: proposal "
                f"{proposal.id} already produced order {existing.client_order_id}"
            )
            await self._refuse(
                proposal,
                f"proposal already executed as order {existing.client_order_id}",
                "internal",
            )
            return "duplicate"

        return None

    async def _submit_with_retries(self, proposal: TradeProposal, quantity: float) -> Order | None:
        """Submit, retrying only transient failures, never duplicating."""
        tracker = self.runtime.order_tracker

        for attempt in range(self.max_retries):
            client_order_id = tracker.client_order_id_for(proposal, attempt)
            request = self._build_request(proposal, quantity, client_order_id)

            try:
                order = await self.runtime.broker.submit_order(request)

            except DuplicateOrder:
                # The broker already has this exact order: an earlier attempt
                # landed even though we did not see the response. Adopt it
                # rather than creating another.
                self.duplicate_prevented_count += 1
                self.warn(
                    f"broker reports {client_order_id} already exists; adopting "
                    f"the existing order instead of resubmitting"
                )
                order = await self._recover_order(client_order_id)
                if order is not None:
                    tracker.record_submission(proposal, order)
                    await self._announce(proposal, order)
                    return order
                await self._refuse(
                    proposal,
                    f"broker reported a duplicate for {client_order_id} but the "
                    f"order could not be retrieved. Check the Alpaca dashboard "
                    f"before retrying.",
                    "broker",
                )
                return None

            except BrokerConnectionError as exc:
                # The dangerous case: we do not know whether the order landed.
                # Ask the broker by client_order_id before doing anything else.
                self.warn(
                    f"connection failure submitting {proposal.symbol} "
                    f"(attempt {attempt + 1}/{self.max_retries}): {exc}"
                )
                recovered = await self._recover_order(client_order_id)
                if recovered is not None:
                    self.info(
                        f"order {client_order_id} did land despite the connection "
                        f"failure; adopting it instead of retrying"
                    )
                    tracker.record_submission(proposal, recovered)
                    await self._announce(proposal, recovered)
                    return recovered

                if attempt + 1 < self.max_retries:
                    await asyncio.sleep(self.retry_backoff * (attempt + 1))
                    continue

                await self._refuse(
                    proposal, f"gave up after {self.max_retries} attempts: {exc}", "broker"
                )
                return None

            except (OrderRejected, BrokerAuthError) as exc:
                # Not retryable: the request itself or the credentials are
                # wrong. Retrying would just repeat the rejection.
                self.rejected_count += 1
                await self.runtime.kill_switch.record_order_rejection(broker=self.runtime.broker)
                await self._refuse(proposal, str(exc), "broker")
                return None

            except BrokerError as exc:
                self.rejected_count += 1
                await self._refuse(proposal, f"unexpected broker error: {exc}", "broker")
                return None

            else:
                tracker.record_submission(proposal, order)
                self.runtime.kill_switch.record_order_success()
                self.submitted_count += 1
                await self._announce(proposal, order)
                return order

        return None

    def _build_request(
        self, proposal: TradeProposal, quantity: float, client_order_id: str
    ) -> OrderRequest:
        """Translate a proposal into a broker order request.

        Bracket legs (stop loss / take profit) are attached only when the
        broker supports them; otherwise the stop is ATLAS's responsibility to
        manage, which is noted in the log so it is not a silent omission.
        """
        capabilities = self.runtime.capabilities
        use_bracket = bool(capabilities and capabilities.supports_bracket_orders)

        request = OrderRequest(
            symbol=proposal.symbol,
            side=proposal.side.value,
            quantity=quantity,
            order_type=proposal.order_type.value,
            time_in_force=proposal.time_in_force.value,
            limit_price=proposal.limit_price,
            stop_price=None,
            client_order_id=client_order_id,
            extended_hours=False,
        )

        if use_bracket:
            request.stop_loss_price = proposal.stop_price
            if proposal.target_price is not None:
                request.take_profit_price = proposal.target_price
        else:
            self.warn(
                f"broker does not support bracket orders; the stop at "
                f"{proposal.stop_price} for {proposal.symbol} is NOT held at the "
                f"broker and must be managed by ATLAS"
            )

        return request

    async def _recover_order(self, client_order_id: str) -> Order | None:
        """Ask the broker whether an order with our id exists.

        This is what turns an ambiguous network failure into a definite answer.
        """
        try:
            return await self.runtime.broker.get_order_by_client_id(client_order_id)
        except BrokerError as exc:
            self.warn(f"could not look up {client_order_id}: {exc}")
            return None

    async def _announce(self, proposal: TradeProposal, order: Order) -> None:
        # Stamp the order with its provenance so the whole chain is traceable
        # from the order back to the strategy that proposed it.
        order.proposal_id = proposal.id
        order.strategy_id = proposal.strategy_id
        order.trace_id = proposal.trace_id

        self.info(
            f"SUBMITTED {order.side.value} {order.quantity:g} {order.symbol} "
            f"({order.order_type.value}) status={order.status.value}"
        )
        await self.publish(
            ExecutionSubmittedEvent(order=order, proposal_id=proposal.id, source=self.id)
        )
        await self.publish(OrderUpdateEvent(order=order, event_type="submitted", source=self.id))

    async def _refuse(self, proposal: TradeProposal, reason: str, stage: str) -> None:
        self.warn(f"execution refused for {proposal.symbol}: {reason}")
        await self.publish(
            ExecutionRejectedEvent(
                proposal_id=proposal.id,
                symbol=proposal.symbol,
                reason=reason,
                stage=stage,  # type: ignore[arg-type]
                source=self.id,
            )
        )

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def detail(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted_count,
            "rejected": self.rejected_count,
            "duplicates_prevented": self.duplicate_prevented_count,
            "tracker": self.runtime.order_tracker.status(),
            "mode": self.runtime.settings.effective_mode.value,
        }
