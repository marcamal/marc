"""Order tracking: idempotency, daily counters and in-flight state.

**The duplicate-order problem.** Submitting an order over HTTP has three
outcomes, not two: success, failure, and *unknown*. A timeout or a dropped
connection leaves the caller unable to tell whether the broker received the
order. A naive retry in that situation opens a second position — the single
most expensive bug an execution system can have.

The fix is a deterministic `client_order_id` per proposal attempt:

*   Every submission carries one.
*   A retry reuses **the same** id, so the broker recognises it and rejects the
    duplicate instead of filling twice.
*   After an ambiguous failure, `ExecutionAgent` asks the broker
    "do you have an order with this id?" via `get_order_by_client_id`. That
    turns "unknown" into a definite answer.

Daily counters live here too, because they must be consistent with what was
actually submitted, and they reset on the US trading day rather than UTC
midnight — a UTC reset would fall in the middle of the US session.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.core.logging import get_logger
from app.models.trading import Order, TradeProposal

log = get_logger(__name__)

#: US market timezone offset used only to decide which trading day it is.
#: Deliberately crude (no DST table): a counter that resets an hour early on
#: some days is harmless, whereas a wrong *session* boundary is not.
_US_EASTERN_OFFSET_HOURS = -5


def trading_day(now: datetime | None = None) -> date:
    """The US trading date for a UTC instant.

    Counters must not reset mid-session, so the day boundary follows the
    exchange rather than UTC.
    """
    moment = now or datetime.now(UTC)
    return (moment + timedelta(hours=_US_EASTERN_OFFSET_HOURS)).date()


class OrderTracker:
    """Per-day order accounting plus the idempotency registry."""

    def __init__(self) -> None:
        self._day: date = trading_day()
        self._count_total = 0
        self._count_by_symbol: dict[str, int] = {}
        self._count_by_strategy: dict[str, int] = {}

        #: client_order_id -> Order, for everything submitted this session.
        self._submitted: dict[str, Order] = {}
        #: proposal_id -> client_order_id, so a replayed proposal cannot
        #: produce a second order.
        self._proposal_to_client_id: dict[str, str] = {}
        #: Attempt counter per proposal, used to build the client id.
        self._attempts: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # day rollover
    # ------------------------------------------------------------------ #

    def _roll_day_if_needed(self, now: datetime | None = None) -> None:
        today = trading_day(now)
        if today != self._day:
            log.info(
                "order counters reset for new trading day",
                extra={
                    "previous_day": str(self._day),
                    "new_day": str(today),
                    "previous_total": self._count_total,
                },
            )
            self._day = today
            self._count_total = 0
            self._count_by_symbol.clear()
            self._count_by_strategy.clear()

    # ------------------------------------------------------------------ #
    # idempotency
    # ------------------------------------------------------------------ #

    def client_order_id_for(self, proposal: TradeProposal, attempt: int = 0) -> str:
        """Deterministic id for a proposal attempt.

        Same proposal and same attempt always produce the same id, which is
        exactly what makes a retry safe.

        Capped at 48 characters, comfortably inside Alpaca's 128-character
        limit, and prefixed `atlas-` so orders placed by ATLAS are instantly
        distinguishable from manual ones in the Alpaca dashboard.
        """
        stem = proposal.id.replace("prop-", "")[:24]
        return f"atlas-{stem}-{attempt}"

    def already_submitted(self, proposal: TradeProposal) -> Order | None:
        """The order this proposal produced, if it already produced one.

        The first line of defence against a proposal being handled twice —
        a duplicated bus event, a retried API call, an agent restart.
        """
        client_id = self._proposal_to_client_id.get(proposal.id)
        if client_id is None:
            return None
        return self._submitted.get(client_id)

    def next_attempt(self, proposal: TradeProposal) -> int:
        return self._attempts.get(proposal.id, 0)

    def record_attempt(self, proposal: TradeProposal) -> int:
        """Increment and return the attempt number for a proposal."""
        attempt = self._attempts.get(proposal.id, 0)
        self._attempts[proposal.id] = attempt + 1
        return attempt

    def record_submission(self, proposal: TradeProposal, order: Order) -> None:
        """Register a successful submission and bump the counters."""
        self._roll_day_if_needed()

        self._submitted[order.client_order_id] = order
        self._proposal_to_client_id[proposal.id] = order.client_order_id

        self._count_total += 1
        self._count_by_symbol[order.symbol] = self._count_by_symbol.get(order.symbol, 0) + 1
        self._count_by_strategy[proposal.strategy_id] = (
            self._count_by_strategy.get(proposal.strategy_id, 0) + 1
        )

        log.info(
            "order recorded",
            extra={
                "client_order_id": order.client_order_id,
                "proposal_id": proposal.id,
                "orders_today": self._count_total,
            },
        )

    def update_order(self, order: Order) -> None:
        """Refresh a tracked order from a broker update (fill, cancel, reject)."""
        if order.client_order_id and order.client_order_id in self._submitted:
            self._submitted[order.client_order_id] = order

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #

    @property
    def orders_today(self) -> int:
        self._roll_day_if_needed()
        return self._count_total

    def orders_today_by_symbol(self) -> dict[str, int]:
        self._roll_day_if_needed()
        return dict(self._count_by_symbol)

    def orders_today_for_strategy(self, strategy_id: str) -> int:
        self._roll_day_if_needed()
        return self._count_by_strategy.get(strategy_id, 0)

    def get_order(self, client_order_id: str) -> Order | None:
        return self._submitted.get(client_order_id)

    def open_orders(self) -> list[Order]:
        return [o for o in self._submitted.values() if o.is_open]

    def all_orders(self) -> list[Order]:
        return sorted(
            self._submitted.values(),
            key=lambda o: o.submitted_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    def status(self) -> dict[str, object]:
        self._roll_day_if_needed()
        return {
            "trading_day": str(self._day),
            "orders_today": self._count_total,
            "orders_by_symbol": dict(self._count_by_symbol),
            "orders_by_strategy": dict(self._count_by_strategy),
            "tracked_orders": len(self._submitted),
            "open_orders": len(self.open_orders()),
        }
