"""The agent framework and the agents.

`build_agents()` is the factory: it reads `agents.yaml`, maps each entry's
`type` onto a class, and registers the instances with the `AgentManager`.
Adding an agent therefore means writing a class, adding it to `AGENT_TYPES`,
and adding a config entry — no changes to the framework itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.agents.base import Agent
from app.agents.execution_agent import ExecutionAgent
from app.agents.manager import AgentManager, AgentManagerError
from app.agents.market_data_agent import MarketDataAgent
from app.agents.mentor_agent import MentorAgent
from app.agents.orchestrator_agent import OrchestratorAgent
from app.agents.portfolio_agent import PortfolioAgent
from app.agents.risk_agent import RiskAgent
from app.agents.scanner_agent import ScannerAgent
from app.agents.strategy_agent import StrategyAgent
from app.agents.technical_agent import TechnicalAgent
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime

log = get_logger(__name__)

#: Maps the `type` field in agents.yaml onto an implementation.
AGENT_TYPES: dict[str, type[Agent]] = {
    "orchestrator": OrchestratorAgent,
    "market_data": MarketDataAgent,
    "portfolio": PortfolioAgent,
    "scanner": ScannerAgent,
    "technical": TechnicalAgent,
    "strategy": StrategyAgent,
    "risk": RiskAgent,
    "execution": ExecutionAgent,
    "mentor": MentorAgent,
}


def build_agents(runtime: AtlasRuntime, manager: AgentManager) -> list[Agent]:
    """Instantiate and register every agent in `agents.yaml`.

    An entry with an unknown `type` is skipped with a clear error rather than
    crashing startup: a typo in one agent's config should not stop the other
    eight from running.
    """
    agents_config = runtime.config.agents
    built: list[Agent] = []

    for entry in agents_config.agents:
        agent_cls = AGENT_TYPES.get(entry.type)
        if agent_cls is None:
            log.error(
                "unknown agent type '%s' for agent '%s'. Known types: %s",
                entry.type,
                entry.id,
                sorted(AGENT_TYPES),
            )
            continue

        agent = agent_cls(
            runtime=runtime,  # type: ignore[call-arg]
            agent_id=entry.id,
            name=entry.name,
            role=entry.role,
            bus=runtime.bus,
            config=entry.config,
            log_buffer_size=agents_config.log_buffer_size,
            autostart=entry.autostart,
        )
        manager.register(agent)
        built.append(agent)

    log.info("built %d agents", len(built))
    return built


__all__ = [
    "AGENT_TYPES",
    "Agent",
    "AgentManager",
    "AgentManagerError",
    "ExecutionAgent",
    "MarketDataAgent",
    "MentorAgent",
    "OrchestratorAgent",
    "PortfolioAgent",
    "RiskAgent",
    "ScannerAgent",
    "StrategyAgent",
    "TechnicalAgent",
    "build_agents",
]
