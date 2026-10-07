"""Risk Agent — the veto authority.

**No order reaches the broker without passing through here.** The Execution
Agent will not act on a proposal that lacks an approval from this agent, and
refuses outright if this agent is not running.

The agent itself does only two things: gather the context (the IO) and record
the verdict (the audit trail). The actual judgement is `app.risk.rules`, which
is pure, deterministic and AI-free. That separation is deliberate — it means
the rules can be tested exhaustively, including tests that deliberately try to
smuggle a bad proposal past them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.brokers.base import BrokerError
from app.config.settings import TradingMode
from app.core.logging import get_logger
from app.events.types import ProposalEvent, RiskDecisionEvent, Topics
from app.models.enums import AssetClass, RiskDecisionType
from app.models.market import MarketClock
from app.models.trading import (
    AccountSnapshot,
    PortfolioSnapshot,
    RiskDecision,
    TradeProposal,
)
from app.risk.rules import RiskContext, RiskEngine

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime

log = get_logger(__name__)


class RiskAgent(Agent):
    """Validates every trade proposal. Holds the kill switch."""

    agent_type = "risk"
    inputs = [Topics.PROPOSAL_CREATED]
    outputs = [Topics.RISK_DECISION, Topics.RISK_EVENT]
    tools = ["risk_engine", "kill_switch", "broker.get_clock"]
    subscriptions = [Topics.PROPOSAL_CREATED]

    def __init__(self, runtime: AtlasRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        self.engine = RiskEngine()

        self.decisions_made = 0
        self.approvals = 0
        self.rejections = 0
        self.reductions = 0
        self.recent_decisions: list[RiskDecision] = []

        # Cached market clock: the clock changes once or twice a day, so
        # fetching it per proposal would waste rate limit for no benefit.
        self._clock: MarketClock | None = None
        self._clock_fetched_at: datetime | None = None
        self._asset_class_cache: dict[str, AssetClass] = {}

    # ------------------------------------------------------------------ #
    # event handling
    # ------------------------------------------------------------------ #

    async def handle_event(self, event: Any) -> None:
        if not isinstance(event, ProposalEvent):
            return
        await self.evaluate(event.proposal)

    async def evaluate(self, proposal: TradeProposal) -> RiskDecision:
        """Judge one proposal and PUBLISH the decision.

        Publishing is what moves the pipeline forward: the Execution Agent is
        subscribed to `risk.decision` and will submit an approved order. Use
        `check()` instead for a read-only evaluation that places nothing.

        Any failure while gathering context produces a REJECTION, not an
        approval. Fail closed: if we cannot establish whether a trade is safe,
        it is not safe.
        """
        self.set_task(f"evaluating {proposal.symbol} from {proposal.strategy_id}")

        try:
            context = await self._build_context(proposal)
        except Exception as exc:
            self.error(f"could not build risk context: {exc}")
            decision = RiskDecision(
                proposal_id=proposal.id,
                decision=RiskDecisionType.REJECTED,
                reasons=[
                    f"context_unavailable: could not establish account or market state "
                    f"({type(exc).__name__}: {exc}). Failing closed."
                ],
                trace_id=proposal.trace_id,
            )
            await self._publish_decision(proposal, decision)
            return decision

        decision = self.engine.evaluate(proposal, context)
        await self._publish_decision(proposal, decision)
        return decision

    async def check(self, proposal: TradeProposal) -> RiskDecision:
        """Evaluate a proposal WITHOUT publishing it. Places no order.

        This is what `/api/risk/check` uses, so that exploring "would this
        trade be allowed?" from the dashboard cannot accidentally execute it.
        The difference from `evaluate()` is exactly one thing: no event is put
        on the bus, so the Execution Agent never sees it.
        """
        context = await self._build_context(proposal)
        return self.engine.evaluate(proposal, context)

    async def _publish_decision(self, proposal: TradeProposal, decision: RiskDecision) -> None:
        self.decisions_made += 1
        if decision.decision is RiskDecisionType.APPROVED:
            self.approvals += 1
        elif decision.decision is RiskDecisionType.APPROVED_REDUCED:
            self.reductions += 1
        else:
            self.rejections += 1

        self.recent_decisions.insert(0, decision)
        del self.recent_decisions[50:]

        if decision.is_approved:
            self.info(f"APPROVED {proposal.symbol}: {decision.summary()}")
        else:
            self.warn(f"VETO {proposal.symbol}: {decision.summary()}")

        await self.publish(RiskDecisionEvent(decision=decision, proposal=proposal, source=self.id))

    # ------------------------------------------------------------------ #
    # context gathering (the only IO in this agent)
    # ------------------------------------------------------------------ #

    async def _build_context(self, proposal: TradeProposal) -> RiskContext:
        runtime = self.runtime

        account = await self._account()
        portfolio = runtime.portfolio_snapshot or PortfolioSnapshot(
            equity=account.equity, cash=account.cash, buying_power=account.buying_power
        )

        capabilities = await runtime.get_capabilities()
        clock = await self._market_clock()
        asset_class = await self._asset_class(proposal.symbol)

        # Fresh quote for the spread and staleness gates. A missing quote is
        # not an error here — the engine treats it as a failed freshness check,
        # which is the correct outcome.
        quote = None
        try:
            if runtime.data_service is not None:
                quote = await runtime.data_service.get_quote(
                    proposal.symbol,
                    max_age_seconds=float(runtime.config.risk.max_data_staleness_seconds),
                )
        except BrokerError as exc:
            self.warn(f"could not fetch quote for {proposal.symbol}: {exc}")

        average_volume = None
        if runtime.data_service is not None:
            bars = await runtime.data_service.get_bars(
                proposal.symbol, timeframe="1Day", limit=30, allow_fetch=False
            )
            if bars:
                average_volume = sum(b.volume for b in bars) / len(bars)

        strategy_config = runtime.strategies.config_for(proposal.strategy_id)
        strategy_risk = strategy_config.risk if strategy_config else None

        strategy_positions = sum(
            1 for p in portfolio.positions if p.strategy_id == proposal.strategy_id
        )

        return RiskContext(
            account=account,
            portfolio=portfolio,
            config=runtime.config.risk,
            mode=runtime.settings.effective_mode,
            capabilities=capabilities,
            clock=clock,
            quote=quote,
            asset_class=asset_class,
            kill_switch_engaged=runtime.kill_switch.is_engaged,
            kill_switch_reason=runtime.kill_switch.reason,
            orders_today=runtime.order_tracker.orders_today,
            orders_today_by_symbol=runtime.order_tracker.orders_today_by_symbol(),
            strategy_open_positions=strategy_positions,
            strategy_orders_today=runtime.order_tracker.orders_today_for_strategy(
                proposal.strategy_id
            ),
            strategy_risk=strategy_risk,
            average_volume=average_volume,
            now=datetime.now(UTC),
        )

    async def _account(self) -> AccountSnapshot:
        """Prefer the Portfolio Agent's cached account, else ask the broker."""
        portfolio_agent = self.runtime.agents.get("portfolio")
        cached = getattr(portfolio_agent, "account", None)
        if cached is not None:
            return cached
        return await self.runtime.broker.get_account()

    async def _market_clock(self, max_age_seconds: float = 60.0) -> MarketClock | None:
        now = datetime.now(UTC)
        if (
            self._clock is not None
            and self._clock_fetched_at is not None
            and (now - self._clock_fetched_at).total_seconds() < max_age_seconds
        ):
            return self._clock
        try:
            self._clock = await self.runtime.broker.get_clock()
            self._clock_fetched_at = now
        except BrokerError as exc:
            self.warn(f"could not fetch market clock: {exc}")
            # Return None, not a guess. The engine refuses to assume the
            # market is open when it cannot confirm it.
            return None
        return self._clock

    async def _asset_class(self, symbol: str) -> AssetClass:
        if symbol in self._asset_class_cache:
            return self._asset_class_cache[symbol]
        try:
            asset_class = await self.runtime.broker.get_asset_class(symbol)
        except BrokerError:
            asset_class = AssetClass.UNSUPPORTED
        self._asset_class_cache[symbol] = asset_class
        return asset_class

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def detail(self) -> dict[str, Any]:
        return {
            "decisions_made": self.decisions_made,
            "approvals": self.approvals,
            "rejections": self.rejections,
            "reductions": self.reductions,
            "kill_switch": self.runtime.kill_switch.status(),
            "mode": self.runtime.settings.effective_mode.value,
            "live_trading_permitted": (self.runtime.settings.effective_mode is TradingMode.LIVE),
        }
