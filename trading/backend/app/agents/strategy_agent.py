"""Strategy Agent — runs enabled strategies and publishes their proposals.

The only thing this agent does with a proposal is put it on the bus. It does
not evaluate it, size it or submit it. From here the proposal travels:

    proposal.created -> Risk Agent -> risk.decision -> Execution Agent -> broker

Three gates must all be open before a single strategy runs:

1.  `strategies_globally_enabled: true` in `strategies.yaml`
2.  that individual strategy's `enabled: true`
3.  ATLAS not in a non-trading mode, and the kill switch clear

All three default to closed. That is the point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.brokers.base import BrokerError
from app.core.trace import trace
from app.events.types import ProposalEvent, Topics
from app.market_data.indicators import compute_indicators
from app.models.trading import TradeProposal
from app.strategies.base import StrategyContext

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime


class StrategyAgent(Agent):
    """Evaluates every active strategy on a timer."""

    agent_type = "strategy"
    inputs = [Topics.MARKET_BAR, Topics.SIGNAL_TECHNICAL]
    outputs = [Topics.PROPOSAL_CREATED]
    tools = ["strategy_registry", "market_data.get_bars", "indicators"]
    subscriptions: list[str] = []

    def __init__(self, runtime: AtlasRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        self.cycle_interval_seconds = float(self.config.get("evaluate_interval_seconds", 60))

        self.proposals_published = 0
        self.last_proposals: list[TradeProposal] = []
        self.skip_reason: str | None = None

    async def run_cycle(self) -> None:
        registry = self.runtime.strategies

        if not registry.globally_enabled:
            self.skip_reason = (
                "strategies_globally_enabled is false in strategies.yaml — no strategy may run"
            )
            self.set_task("idle: strategies globally disabled")
            return

        active = registry.active
        if not active:
            self.skip_reason = "no strategy is enabled"
            self.set_task("idle: no strategy enabled")
            return

        if self.runtime.kill_switch.is_engaged:
            self.skip_reason = f"kill switch engaged: {self.runtime.kill_switch.reason}"
            self.set_task("idle: kill switch engaged")
            return

        self.skip_reason = None
        self.set_task(f"evaluating {len(active)} active strategies")

        published: list[TradeProposal] = []
        for strategy in active:
            try:
                context = await self._build_context(strategy.symbols, strategy.timeframe)
            except BrokerError as exc:
                self.warn(f"could not build context for {strategy.id}: {exc}")
                continue

            try:
                proposals = strategy.generate(context)
            except Exception as exc:
                strategy.last_error = str(exc)
                self.error(f"strategy {strategy.id} raised: {type(exc).__name__}: {exc}")
                self.log.exception("strategy generate() failed", extra={"strategy": strategy.id})
                continue

            for proposal in proposals:
                # Each proposal gets its own trace, so the whole chain from
                # here to the fill can be retrieved as one story.
                with trace(prefix="trade") as trace_id:
                    proposal.trace_id = trace_id
                    self.info(
                        f"proposal: {proposal.side.value} {proposal.quantity:g} "
                        f"{proposal.symbol} @ {proposal.entry_price:.2f} "
                        f"stop {proposal.stop_price:.2f} "
                        f"(risk ${proposal.estimated_risk:.2f}) "
                        f"from {strategy.id}"
                    )
                    await self.publish(ProposalEvent(proposal=proposal, source=self.id))
                    published.append(proposal)
                    self.proposals_published += 1

        self.last_proposals = published
        if published:
            self.confidence = max(p.confidence for p in published)
            self.set_task(f"{len(published)} proposals published this cycle")
        else:
            self.set_task(f"{len(active)} strategies evaluated, no setups found")

    async def _build_context(self, symbols: list[str], timeframe: str) -> StrategyContext:
        """Assemble the context a strategy sees.

        The same shape the backtesting engine will build from historical data,
        which is what keeps one strategy implementation valid in both places.
        """
        runtime = self.runtime
        service = runtime.data_service

        account = await runtime.broker.get_account()
        portfolio = runtime.portfolio_snapshot
        positions = portfolio.positions if portfolio else await runtime.broker.get_positions()

        bars = (
            await service.get_bars_multi(symbols, timeframe=timeframe, limit=200) if service else {}
        )

        indicator_config = runtime.config.scanner.indicators
        indicators = {
            symbol: compute_indicators(
                symbol=symbol,
                bars=symbol_bars,
                timeframe=timeframe,
                ema_fast=indicator_config.ema_fast,
                ema_slow=indicator_config.ema_slow,
                sma_long=indicator_config.sma_long,
                rsi_period=indicator_config.rsi_period,
                atr_period=indicator_config.atr_period,
                bollinger_period=indicator_config.bollinger_period,
                bollinger_std=indicator_config.bollinger_std,
                momentum_period=indicator_config.momentum_period,
            )
            for symbol, symbol_bars in bars.items()
            if symbol_bars
        }

        clock = runtime.market_clock
        return StrategyContext(
            account=account,
            positions=positions,
            bars=bars,
            indicators=indicators,
            market_open=clock.is_open if clock else False,
            seconds_until_close=clock.seconds_until_close if clock else None,
        )

    def detail(self) -> dict[str, Any]:
        return {
            "proposals_published": self.proposals_published,
            "skip_reason": self.skip_reason,
            "registry": self.runtime.strategies.status(),
            "last_proposals": [
                {
                    "id": p.id,
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "quantity": p.quantity,
                    "entry": p.entry_price,
                    "stop": p.stop_price,
                    "target": p.target_price,
                    "risk": round(p.estimated_risk, 2),
                    "confidence": p.confidence,
                }
                for p in self.last_proposals
            ],
        }
