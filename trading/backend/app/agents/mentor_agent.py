"""Mentor Agent — explains what happened and why, in plain English.

This agent exists because the operator said their trading skills had faded and
they wanted to learn again. Its output is aimed at a person, not a machine.

It explains:

*   why a risk veto happened, and what the limit actually protects against
*   what a technical signal detected, and what would invalidate it
*   what a fill means for the position and the account
*   after a closed trade, a structured review

**Everything here is deterministic template text, not an LLM.** That is a
considered choice for Phase 1: an explanation of a risk rejection must state
the real limit and the real observed value, every time, with no chance of a
plausible-sounding invention. Adding an LLM to rephrase these more naturally
comes later, through `app.ai`'s provider interface — on top of the facts
assembled here, never instead of them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.events.types import (
    ExecutionSubmittedEvent,
    MentorExplanationEvent,
    RiskDecisionEvent,
    RiskEvent,
    SignalEvent,
    Topics,
)
from app.models.enums import SignalDirection

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime

#: Plain-English explanation of what each risk rule is protecting you from.
#: The rule name alone ("max_correlated_exposure") teaches nothing.
_RULE_LESSONS: dict[str, str] = {
    "kill_switch": (
        "The kill switch is on, so nothing can trade. That is working as intended — "
        "it exists so you can stop everything instantly without trusting the software."
    ),
    "max_risk_per_trade": (
        "Risk per trade is the foundation of survival. Risking a small fixed "
        "percentage means a losing streak is survivable: ten consecutive losses at "
        "0.5% costs about 5% of the account, which is recoverable. The same streak "
        "at 5% per trade costs roughly 40%, which usually is not."
    ),
    "max_position_size": (
        "Even with a tight stop, one position should not dominate the account. A gap "
        "through your stop — an overnight earnings miss, say — can cost far more than "
        "the planned risk, and position size is what caps that damage."
    ),
    "max_daily_loss": (
        "A daily loss limit stops the single most destructive pattern in trading: "
        "losing, then trading bigger to win it back. Stopping for the day is a "
        "decision made in advance by a calm version of you."
    ),
    "max_portfolio_drawdown": (
        "Drawdown is measured from your equity high, not your deposits. Recovering "
        "from a 20% drawdown needs a 25% gain, and from 50% needs 100%. Keeping "
        "drawdowns small keeps the arithmetic of recovery on your side."
    ),
    "max_orders_per_day": (
        "An order-count limit is a circuit breaker against a software bug. A loop "
        "that submits orders in a tight cycle can do enormous damage in seconds, and "
        "no strategy legitimately needs dozens of orders a day."
    ),
    "max_orders_per_symbol_per_day": (
        "Repeatedly trading the same symbol in one day is usually churn rather than "
        "edge. In Germany it also matters for tax: every realised gain is taxable at "
        "roughly 26.4%, so frequent round trips lose money to tax that a held "
        "position would not."
    ),
    "data_freshness": (
        "Trading on a stale price means trading a market that no longer exists. When "
        "the feed is late, the correct action is to do nothing — a missed trade costs "
        "nothing, a trade at an imaginary price can cost a lot."
    ),
    "spread": (
        "The spread is a cost you pay on entry and again on exit. On a wide spread, a "
        "trade can need a meaningful move just to break even, which quietly destroys "
        "the edge of any short-term strategy."
    ),
    "liquidity": (
        "In a thin symbol, getting out is the problem. Your own order moves the price, "
        "and the exit you planned may not exist at the size you need."
    ),
    "market_open": (
        "Outside regular hours, spreads widen and volume thins, so the fill you get "
        "may be far from the price you saw."
    ),
    "max_total_exposure": (
        "Total exposure is what connects individual trades into one risk. Eight "
        "positions that all fall together on a bad day is really one large position."
    ),
    "max_correlated_exposure": (
        "Correlated positions are not diversification. Holding NVDA, AMD, AVGO and "
        "SMH is four ways to own the same semiconductor bet, and they will fall "
        "together."
    ),
    "max_open_positions": (
        "Each open position needs attention. Beyond a handful, most people stop "
        "managing them properly, and unmanaged positions drift into losses."
    ),
    "buying_power": (
        "Not enough settled cash. On a cash account, proceeds from a sale take a day "
        "to settle before they can be reused."
    ),
    "reward_risk_ratio": (
        "If you risk 1 to make 1, you need to be right more than half the time just "
        "to break even after costs. Asking for at least 1.5 to 1 means you can be "
        "wrong more often than right and still come out ahead."
    ),
    "broker_reconciled": (
        "ATLAS and the broker disagree about what you hold. Until that is resolved, "
        "any position size calculated here is based on a number that might be wrong."
    ),
    "short_selling": (
        "Shorting needs a margin account and borrowed shares. On a cash account it is "
        "simply not available — and its risk profile is different in kind, because "
        "losses are not capped."
    ),
    "asset_supported": (
        "This instrument is not tradable on this account. Availability depends on the "
        "broker and on your country of residence, which is why ATLAS checks rather "
        "than assumes."
    ),
    "stop_distance": (
        "A stop placed almost on top of the entry is not risk management: ordinary "
        "noise will take you out, and the position size it implies is enormous."
    ),
}


class MentorAgent(Agent):
    """Turns system events into teaching material."""

    agent_type = "mentor"
    inputs = [Topics.RISK_DECISION, Topics.SIGNAL_TECHNICAL, Topics.EXECUTION_SUBMITTED]
    outputs = [Topics.MENTOR_EXPLANATION, Topics.JOURNAL_ENTRY]
    tools = ["templates"]
    subscriptions = [
        Topics.RISK_DECISION,
        Topics.SIGNAL_TECHNICAL,
        Topics.EXECUTION_SUBMITTED,
        Topics.RISK_EVENT,
    ]

    def __init__(self, runtime: AtlasRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        self.explain_signals = bool(self.config.get("explain_signals", True))
        self.explain_rejections = bool(self.config.get("explain_rejections", True))
        self.explanations: list[MentorExplanationEvent] = []

    async def handle_event(self, event: Any) -> None:
        if isinstance(event, RiskDecisionEvent):
            await self._explain_risk_decision(event)
        elif isinstance(event, SignalEvent) and self.explain_signals:
            await self._explain_signal(event)
        elif isinstance(event, ExecutionSubmittedEvent):
            await self._explain_order(event)
        elif isinstance(event, RiskEvent):
            await self._explain_risk_event(event)

    async def _emit(self, explanation: MentorExplanationEvent) -> None:
        self.explanations.insert(0, explanation)
        del self.explanations[100:]
        await self.publish(explanation)

    # ------------------------------------------------------------------ #
    # explanations
    # ------------------------------------------------------------------ #

    async def _explain_risk_decision(self, event: RiskDecisionEvent) -> None:
        decision = event.decision
        proposal = event.proposal

        if decision.is_approved:
            lines = [
                f"The Risk Officer approved a {proposal.side.value} of "
                f"{decision.approved_quantity or proposal.quantity:g} {proposal.symbol} "
                f"at about {proposal.entry_price:.2f}.",
                "",
                f"Stop: {proposal.stop_price:.2f} ({proposal.risk_per_share:.2f} per share away).",
                f"If the stop is hit, the loss is about ${proposal.estimated_risk:,.2f}.",
            ]
            if proposal.target_price and proposal.reward_risk_ratio:
                lines.append(
                    f"Target: {proposal.target_price:.2f}, which is "
                    f"{proposal.reward_risk_ratio:.1f} times the risk."
                )
            if proposal.invalidation:
                lines += ["", f"What would prove this wrong: {proposal.invalidation}"]
            if proposal.evidence:
                lines += ["", "Why the strategy liked it:"]
                lines += [f"  - {e.detail}" for e in proposal.evidence]

            await self._emit(
                MentorExplanationEvent(
                    title=f"Trade approved: {proposal.symbol}",
                    body="\n".join(lines),
                    symbol=proposal.symbol,
                    category="signal",
                    lessons=[
                        "Position size came from the stop distance, not from a hunch.",
                        "The invalidation condition was written before the entry, "
                        "which is what stops a loss from becoming a story.",
                    ],
                    source=self.id,
                )
            )
            return

        if not self.explain_rejections:
            return

        failed = [c for c in decision.checks if not c.passed]
        lines = [
            f"The Risk Officer blocked a {proposal.side.value} of "
            f"{proposal.quantity:g} {proposal.symbol}.",
            "",
            "This is the system working. Here is what failed:",
        ]
        lessons: list[str] = []
        for check in failed:
            lines.append(f"\n  {check.rule}: {check.detail}")
            lesson = _RULE_LESSONS.get(check.rule)
            if lesson:
                lines.append(f"      Why this rule exists: {lesson}")
                lessons.append(lesson)

        await self._emit(
            MentorExplanationEvent(
                title=f"Trade blocked: {proposal.symbol}",
                body="\n".join(lines),
                symbol=proposal.symbol,
                category="rejection",
                lessons=lessons[:3],
                source=self.id,
            )
        )

    async def _explain_signal(self, event: SignalEvent) -> None:
        signal = event.signal
        # Neutral or weak signals are not worth a teaching moment; they would
        # drown the interesting ones.
        if signal.direction is SignalDirection.NEUTRAL or signal.confidence < 0.5:
            return

        lines = [
            f"{signal.symbol} looks {signal.direction.value} "
            f"({signal.confidence:.0%} confidence, score {signal.score:.0f}/100).",
            "",
            "What the indicators said:",
        ]
        lines += [f"  - {e.source}: {e.detail}" for e in signal.evidence[:10]]

        if signal.risk_flags:
            lines += ["", "Warning flags:"]
            lines += [f"  - {flag.replace('_', ' ')}" for flag in signal.risk_flags]

        lines += [
            "",
            "Remember: a signal is an observation, not a trade. It becomes a trade "
            "only after a strategy defines an entry, a stop and a size, and the Risk "
            "Officer approves it.",
        ]

        await self._emit(
            MentorExplanationEvent(
                title=f"Signal: {signal.symbol} {signal.direction.value}",
                body="\n".join(lines),
                symbol=signal.symbol,
                category="signal",
                lessons=[
                    "Higher timeframes are weighted more heavily: a 5-minute signal "
                    "fighting the daily trend is a poor bet.",
                ],
                source=self.id,
            )
        )

    async def _explain_order(self, event: ExecutionSubmittedEvent) -> None:
        order = event.order
        lines = [
            f"Order sent: {order.side.value} {order.quantity:g} {order.symbol} "
            f"({order.order_type.value}).",
            f"Broker status: {order.status.value}.",
            "",
            f"Mode: {self.runtime.settings.effective_mode.value.upper()}.",
        ]
        if self.runtime.settings.effective_mode.value != "live":
            lines.append("No real money is involved. This is a simulation.")

        await self._emit(
            MentorExplanationEvent(
                title=f"Order submitted: {order.symbol}",
                body="\n".join(lines),
                symbol=order.symbol,
                category="fill",
                source=self.id,
            )
        )

    async def _explain_risk_event(self, event: RiskEvent) -> None:
        if event.severity == "info":
            return
        lesson = _RULE_LESSONS.get(event.rule, "")
        body = [f"Risk event ({event.severity}): {event.detail}"]
        if event.observed is not None and event.limit is not None:
            body.append(f"\nObserved {event.observed:.2f} against a limit of {event.limit:.2f}.")
        if lesson:
            body.append(f"\nWhy this matters: {lesson}")

        await self._emit(
            MentorExplanationEvent(
                title=f"Risk: {event.rule.replace('_', ' ')}",
                body="\n".join(body),
                category="risk",
                lessons=[lesson] if lesson else [],
                source=self.id,
            )
        )

    # ------------------------------------------------------------------ #
    # trade review
    # ------------------------------------------------------------------ #

    def build_trade_review(
        self,
        symbol: str,
        strategy_id: str,
        entry_price: float,
        exit_price: float,
        quantity: float,
        planned_stop: float,
        planned_target: float | None,
        thesis: str,
        opened_at: datetime,
        closed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """A structured review of one closed trade.

        Returned as data so it can be stored in the journal and rendered in
        the UI. The questions are fixed on purpose: a consistent template is
        what makes a journal searchable and comparable over months.
        """
        closed = closed_at or datetime.now(UTC)
        pl = (exit_price - entry_price) * quantity
        planned_risk = abs(entry_price - planned_stop) * quantity
        r_multiple = (pl / planned_risk) if planned_risk > 0 else None

        strengths: list[str] = []
        mistakes: list[str] = []

        if planned_risk > 0 and pl < -planned_risk * 1.15:
            mistakes.append(
                f"The loss (${abs(pl):,.2f}) exceeded the planned risk "
                f"(${planned_risk:,.2f}) by more than 15%. Either the stop was not "
                f"honoured, or slippage was worse than assumed. Worth checking which."
            )
        else:
            strengths.append("The loss stayed within the planned risk.")

        if planned_target and pl > 0 and exit_price < planned_target:
            mistakes.append(
                "Exited before the target. Taking profit early feels safe but caps "
                "the winners that are supposed to pay for the losers."
            )

        if r_multiple is not None and r_multiple >= 1.5:
            strengths.append(f"Good result: {r_multiple:.2f}R on the planned risk.")

        return {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "opened_at": opened_at.isoformat(),
            "closed_at": closed.isoformat(),
            "holding_period_minutes": round((closed - opened_at).total_seconds() / 60, 1),
            "setup": thesis,
            "entry": entry_price,
            "exit": exit_price,
            "quantity": quantity,
            "planned_stop": planned_stop,
            "planned_target": planned_target,
            "profit_loss": round(pl, 2),
            "planned_risk": round(planned_risk, 2),
            "r_multiple": round(r_multiple, 2) if r_multiple is not None else None,
            "outcome": "win" if pl > 0 else "loss" if pl < 0 else "scratch",
            "strengths": strengths,
            "mistakes": mistakes,
            "lessons": [
                "Judge the decision, not the outcome. A good decision can lose and a "
                "bad one can win.",
                "One trade tells you almost nothing. Thirty trades of the same setup "
                "start to tell you something.",
            ],
        }

    def detail(self) -> dict[str, Any]:
        return {
            "explanations": len(self.explanations),
            "recent": [
                {
                    "title": e.title,
                    "category": e.category,
                    "symbol": e.symbol,
                    "timestamp": e.timestamp.isoformat(),
                    "body": e.body,
                    "lessons": e.lessons,
                }
                for e in self.explanations[:20]
            ],
        }
