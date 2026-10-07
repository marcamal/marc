"""Portfolio Agent — mirrors the broker and tracks exposure, P&L, drawdown.

Reconciles against the broker on a timer. **Broker state always wins.** When
ATLAS and Alpaca disagree, ATLAS adopts Alpaca's numbers, flags the mismatch,
and the Risk Agent stops approving new trades until it clears — because
position sizing computed from a wrong position is worse than no trade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.brokers.base import BrokerError
from app.events.types import (
    AccountUpdateEvent,
    OrderUpdateEvent,
    PortfolioSnapshotEvent,
    RiskEvent,
    Topics,
)
from app.models.enums import OrderStatus, Side
from app.models.trading import AccountSnapshot, PortfolioSnapshot
from app.portfolio.snapshot import build_snapshot

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime


class PortfolioAgent(Agent):
    """Keeps a live, reconciled picture of the account."""

    agent_type = "portfolio"
    inputs = [Topics.ORDER_UPDATE]
    outputs = [Topics.PORTFOLIO_SNAPSHOT, Topics.ACCOUNT_UPDATE, Topics.RISK_EVENT]
    tools = ["broker.get_account", "broker.get_positions"]
    subscriptions = [Topics.ORDER_UPDATE]

    def __init__(self, runtime: AtlasRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        self.cycle_interval_seconds = float(self.config.get("reconcile_interval_seconds", 30))

        self.account: AccountSnapshot | None = None
        self.snapshot: PortfolioSnapshot | None = None
        self.high_water_mark: float = 0.0
        self.realized_pl_today: float = 0.0
        #: What ATLAS believes it holds, built from its own fills. Compared
        #: against the broker to detect divergence.
        self.internal_positions: dict[str, float] = {}
        self._mismatch_reported = False

    async def on_start(self) -> None:
        # Reconcile immediately so the dashboard is populated at startup
        # rather than blank for the first cycle interval.
        await self.run_cycle()

    async def run_cycle(self) -> None:
        self.set_task("reconciling with broker")
        try:
            account = await self.runtime.broker.get_account()
            positions = await self.runtime.broker.get_positions()
        except BrokerError as exc:
            self.warn(f"could not reach broker for reconciliation: {exc}")
            raise

        self.account = account
        self.high_water_mark = max(self.high_water_mark, account.equity)

        # Positions ATLAS never opened are normal (manual trades, earlier
        # sessions). Seed the internal view from the broker on the first pass
        # so the very first reconciliation does not report a false mismatch.
        if not self.internal_positions and positions:
            self.internal_positions = {p.symbol: p.quantity for p in positions}

        snapshot = build_snapshot(
            account=account,
            positions=positions,
            config=self.runtime.config.risk,
            high_water_mark=self.high_water_mark,
            realized_pl_today=self.realized_pl_today,
            internal_positions=self.internal_positions,
        )
        self.snapshot = snapshot
        self.runtime.portfolio_snapshot = snapshot

        await self.publish(AccountUpdateEvent(account=account, source=self.id))
        await self.publish(PortfolioSnapshotEvent(snapshot=snapshot, source=self.id))

        await self._handle_mismatch(snapshot)
        await self._check_risk_thresholds(snapshot)

        self.set_task(
            f"equity ${account.equity:,.2f} | {snapshot.open_position_count} positions "
            f"| day {snapshot.daily_pl_pct:+.2f}%"
        )

    async def _handle_mismatch(self, snapshot: PortfolioSnapshot) -> None:
        """Report a broker/ATLAS divergence, then adopt the broker's view."""
        if not snapshot.reconciliation_mismatch:
            self._mismatch_reported = False
            return

        if not self._mismatch_reported:
            self._mismatch_reported = True
            detail = "; ".join(snapshot.mismatch_detail[:5])
            self.warn(f"BROKER RECONCILIATION MISMATCH: {detail}")
            await self.publish(
                RiskEvent(
                    severity="critical",
                    rule="broker_reconciliation",
                    detail=(
                        f"ATLAS and the broker disagree about positions. Broker state is "
                        f"authoritative and has been adopted. New orders are blocked until "
                        f"this clears. Details: {detail}"
                    ),
                    source=self.id,
                )
            )
            await self.runtime.kill_switch.check_auto_trigger(
                "broker_reconciliation_mismatch",
                f"Position mismatch with broker: {detail}",
                broker=self.runtime.broker,
            )

        # Broker wins: adopt its numbers so the next cycle starts from truth.
        self.internal_positions = {p.symbol: p.quantity for p in snapshot.positions}

    async def _check_risk_thresholds(self, snapshot: PortfolioSnapshot) -> None:
        """Engage the kill switch on a breached daily loss or drawdown."""
        risk_config = self.runtime.config.risk

        daily_loss_pct = -min(0.0, snapshot.daily_pl_pct)
        if daily_loss_pct >= risk_config.max_daily_loss_pct:
            await self.publish(
                RiskEvent(
                    severity="critical",
                    rule="max_daily_loss",
                    detail=(
                        f"Daily loss {daily_loss_pct:.2f}% has reached the "
                        f"{risk_config.max_daily_loss_pct:.2f}% limit."
                    ),
                    observed=daily_loss_pct,
                    limit=risk_config.max_daily_loss_pct,
                    source=self.id,
                )
            )
            await self.runtime.kill_switch.check_auto_trigger(
                "daily_loss_breached",
                f"Daily loss limit breached: down {daily_loss_pct:.2f}% "
                f"(limit {risk_config.max_daily_loss_pct:.2f}%)",
                broker=self.runtime.broker,
            )

        if snapshot.drawdown_pct >= risk_config.max_portfolio_drawdown_pct:
            await self.publish(
                RiskEvent(
                    severity="critical",
                    rule="max_portfolio_drawdown",
                    detail=(
                        f"Drawdown {snapshot.drawdown_pct:.2f}% from the high water mark "
                        f"has reached the {risk_config.max_portfolio_drawdown_pct:.2f}% limit."
                    ),
                    observed=snapshot.drawdown_pct,
                    limit=risk_config.max_portfolio_drawdown_pct,
                    source=self.id,
                )
            )
            await self.runtime.kill_switch.check_auto_trigger(
                "portfolio_drawdown_breached",
                f"Drawdown limit breached: {snapshot.drawdown_pct:.2f}% "
                f"(limit {risk_config.max_portfolio_drawdown_pct:.2f}%)",
                broker=self.runtime.broker,
            )

    async def handle_event(self, event: Any) -> None:
        """Update the internal position view from fills."""
        if not isinstance(event, OrderUpdateEvent):
            return

        order = event.order
        if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return
        if order.filled_quantity <= 0:
            return

        delta = order.filled_quantity * (1 if order.side is Side.BUY else -1)
        current = self.internal_positions.get(order.symbol, 0.0)
        updated = current + delta

        if abs(updated) < 1e-9:
            self.internal_positions.pop(order.symbol, None)
        else:
            self.internal_positions[order.symbol] = updated

        # Realised P&L on a closing trade. A full cost-basis engine with
        # German tax lots comes later; this is enough for a daily figure.
        if order.side is Side.SELL and order.average_fill_price and self.snapshot:
            position = next((p for p in self.snapshot.positions if p.symbol == order.symbol), None)
            if position:
                self.realized_pl_today += (
                    order.average_fill_price - position.average_entry_price
                ) * order.filled_quantity

        self.info(
            f"position updated from fill: {order.symbol} "
            f"{'+' if delta > 0 else ''}{delta:g} -> {updated:g}"
        )
