"""Orchestrator Agent — coordinator and health watchdog.

What it does:

*   watches every other agent's health and reports degradation
*   aggregates the latest findings (scanner, signals, portfolio) into one
    briefing the Command Center can render
*   prevents duplicate research: when the scanner surfaces the same symbol
    repeatedly, the technical agent is not asked to re-analyse it every cycle
*   escalates system-level problems

What it deliberately does **not** do: place trades. It has no path to the
broker, and the brief was explicit that the coordinator must not be the thing
that trades. Keeping the coordinator out of the order path means a bug in
coordination logic cannot become a bug in execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.events.types import (
    ScannerResultsEvent,
    SignalEvent,
    SystemEvent,
    Topics,
)
from app.models.enums import AgentStatus, SignalDirection

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime


class OrchestratorAgent(Agent):
    """Coordinates the agent network and summarises its state."""

    agent_type = "orchestrator"
    inputs = [Topics.SCANNER_RESULTS, Topics.SIGNAL_ANY, Topics.AGENT_STATUS]
    outputs = [Topics.SYSTEM_ERROR]
    tools = ["agent_manager"]
    subscriptions = [Topics.SCANNER_RESULTS, Topics.SIGNAL_ANY]

    def __init__(self, runtime: AtlasRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        self.cycle_interval_seconds = float(self.config.get("research_cycle_seconds", 300))

        #: symbol -> when it was last analysed, for duplicate-work suppression.
        self._last_analysed: dict[str, datetime] = {}
        self._reanalyse_after_seconds = 300.0

        self.top_candidates: list[str] = []
        self.strong_signals: list[dict[str, Any]] = []
        self._reported_unhealthy: set[str] = set()

    async def run_cycle(self) -> None:
        self.set_task("checking agent health")
        await self._check_agent_health()
        self.set_task(self._summary_line())

    async def _check_agent_health(self) -> None:
        """Report agents that have gone unhealthy, once each."""
        summary = self.runtime.agents.health_summary()
        unhealthy = set(summary.get("unhealthy_ids", []))  # type: ignore[arg-type]

        newly_unhealthy = unhealthy - self._reported_unhealthy
        for agent_id in newly_unhealthy:
            agent = self.runtime.agents.get(agent_id)
            detail = (agent.stats.last_error if agent else None) or "no detail"
            self.warn(f"agent '{agent_id}' is unhealthy: {detail}")
            await self.publish(
                SystemEvent(
                    topic=Topics.SYSTEM_ERROR,
                    detail=f"Agent '{agent_id}' is unhealthy: {detail}",
                    context={"agent": agent_id, "status": agent.status.value if agent else "?"},
                    source=self.id,
                )
            )

        recovered = self._reported_unhealthy - unhealthy
        for agent_id in recovered:
            self.info(f"agent '{agent_id}' has recovered")

        self._reported_unhealthy = unhealthy

    async def handle_event(self, event: Any) -> None:
        if isinstance(event, ScannerResultsEvent):
            self.top_candidates = [r.symbol for r in event.results]
            self._note_analysis(self.top_candidates)
            return

        if isinstance(event, SignalEvent):
            signal = event.signal
            if signal.direction is not SignalDirection.NEUTRAL and signal.confidence >= 0.5:
                self.strong_signals.insert(
                    0,
                    {
                        "symbol": signal.symbol,
                        "direction": signal.direction.value,
                        "confidence": signal.confidence,
                        "score": signal.score,
                        "agent": signal.agent_id,
                        "timestamp": signal.created_at.isoformat(),
                    },
                )
                del self.strong_signals[20:]

    def _note_analysis(self, symbols: list[str]) -> None:
        now = datetime.now(UTC)
        for symbol in symbols:
            self._last_analysed[symbol] = now

    def needs_analysis(self, symbol: str) -> bool:
        """Should this symbol be re-analysed, or is a recent result good enough?

        Re-running a full multi-timeframe analysis on an unchanged symbol
        costs API calls and tells us nothing new.
        """
        last = self._last_analysed.get(symbol)
        if last is None:
            return True
        return (datetime.now(UTC) - last).total_seconds() > self._reanalyse_after_seconds

    def _summary_line(self) -> str:
        health = self.runtime.agents.health_summary()
        return f"{health['running']}/{health['registered']} agents running" + (
            f", {health['unhealthy']} unhealthy" if health["unhealthy"] else ""
        )

    def briefing(self) -> dict[str, Any]:
        """The aggregated view the Command Center renders."""
        runtime = self.runtime
        portfolio = runtime.portfolio_snapshot
        clock = runtime.market_clock

        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": runtime.settings.effective_mode.value,
            "market_open": clock.is_open if clock else None,
            "agents": runtime.agents.health_summary(),
            "kill_switch": runtime.kill_switch.status(),
            "portfolio": (
                {
                    "equity": portfolio.equity,
                    "daily_pl": portfolio.daily_pl,
                    "daily_pl_pct": portfolio.daily_pl_pct,
                    "open_positions": portfolio.open_position_count,
                    "total_exposure_pct": portfolio.total_exposure_pct,
                    "drawdown_pct": portfolio.drawdown_pct,
                }
                if portfolio
                else None
            ),
            "top_candidates": self.top_candidates[:10],
            "strong_signals": self.strong_signals[:10],
            "strategies": {
                "globally_enabled": runtime.strategies.globally_enabled,
                "active": [s.id for s in runtime.strategies.active],
                "loaded": len(runtime.strategies.all),
            },
        }

    def detail(self) -> dict[str, Any]:
        return {
            "top_candidates": self.top_candidates,
            "strong_signals": self.strong_signals[:10],
            "agents_unhealthy": sorted(self._reported_unhealthy),
            "symbols_tracked": len(self._last_analysed),
        }


#: Convenience export so the API can show the canonical pipeline even before
#: the agents have produced any edges.
PIPELINE_ORDER = [
    "orchestrator",
    "market_data",
    "scanner",
    "technical",
    "strategy",
    "risk",
    "execution",
    "broker",
]


def agent_status_colour(status: AgentStatus) -> str:
    """Map an agent status onto a UI colour token."""
    return {
        AgentStatus.WORKING: "active",
        AgentStatus.IDLE: "ok",
        AgentStatus.STARTING: "pending",
        AgentStatus.STOPPING: "pending",
        AgentStatus.STOPPED: "off",
        AgentStatus.CREATED: "off",
        AgentStatus.ERROR: "error",
        AgentStatus.UNHEALTHY: "warn",
    }.get(status, "off")
