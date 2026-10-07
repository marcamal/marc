"""AgentManager — registry, supervisor and health monitor.

Responsibilities:

*   hold the agent registry and start/stop agents on request
*   honour `autostart` from `agents.yaml`
*   watch heartbeats and mark silent agents UNHEALTHY
*   build the agent network graph the dashboard renders
*   refuse to stop agents that ATLAS must not run without

That last point matters: the Risk Agent and the Execution Agent are
*protected*. Stopping the Risk Agent from the UI would leave the Execution
Agent without a veto authority, so the manager refuses, and the Execution
Agent independently fails closed if the Risk Agent is ever absent. Two
mechanisms for one invariant, because this is the one that must not break.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.agents.base import Agent
from app.core.logging import get_logger
from app.events.bus import EventBus
from app.events.types import topic_matches
from app.models.agent import AgentDescriptor, AgentGraph, AgentGraphEdge, AgentGraphNode
from app.models.enums import AgentStatus

log = get_logger(__name__)

#: Agents that may not be stopped while ATLAS is running.
PROTECTED_AGENT_IDS = frozenset({"risk", "execution"})


class AgentManagerError(RuntimeError):
    """Raised on an invalid lifecycle request."""


class AgentManager:
    """Owns the agents."""

    def __init__(
        self,
        bus: EventBus,
        health_check_interval_seconds: float = 15.0,
        heartbeat_timeout_seconds: float = 90.0,
    ) -> None:
        self.bus = bus
        self._agents: dict[str, Agent] = {}
        self._health_interval = health_check_interval_seconds
        self._heartbeat_timeout = heartbeat_timeout_seconds
        self._monitor: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # registry
    # ------------------------------------------------------------------ #

    def register(self, agent: Agent) -> Agent:
        if agent.id in self._agents:
            raise AgentManagerError(f"agent id '{agent.id}' is already registered")
        self._agents[agent.id] = agent
        log.info(
            "agent registered",
            extra={"agent": agent.id, "type": agent.agent_type, "autostart": agent.autostart},
        )
        return agent

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def require(self, agent_id: str) -> Agent:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise AgentManagerError(f"no agent registered with id '{agent_id}'")
        return agent

    @property
    def agents(self) -> list[Agent]:
        return list(self._agents.values())

    def by_type(self, agent_type: str) -> list[Agent]:
        return [a for a in self._agents.values() if a.agent_type == agent_type]

    def is_running(self, agent_id: str) -> bool:
        agent = self._agents.get(agent_id)
        return bool(agent and agent.status.is_running)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def start_all(self) -> None:
        """Start every agent marked `autostart`, then begin health monitoring.

        Agents start sequentially rather than concurrently: the Risk Agent
        must be live before the Execution Agent accepts anything, and a
        deterministic order makes startup logs readable.
        """
        for agent in self._agents.values():
            if agent.autostart:
                await self.start_agent(agent.id)

        self._monitor = asyncio.create_task(self._health_loop(), name="agent-health-monitor")
        log.info(
            "agent manager started",
            extra={
                "registered": len(self._agents),
                "running": sum(1 for a in self._agents.values() if a.status.is_running),
            },
        )

    async def stop_all(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            try:
                await self._monitor
            except asyncio.CancelledError:
                pass
            self._monitor = None

        # Stop in reverse registration order, so producers stop before the
        # consumers that depend on them are torn down.
        for agent in reversed(list(self._agents.values())):
            try:
                await agent.stop()
            except Exception:
                log.exception("failed to stop agent", extra={"agent": agent.id})
        log.info("agent manager stopped")

    async def start_agent(self, agent_id: str) -> Agent:
        agent = self.require(agent_id)
        await agent.start()
        return agent

    async def stop_agent(self, agent_id: str, force: bool = False) -> Agent:
        """Stop one agent.

        Protected agents refuse unless `force=True`, which is not exposed over
        the API — only used during full shutdown.
        """
        agent = self.require(agent_id)
        if agent_id in PROTECTED_AGENT_IDS and not force:
            raise AgentManagerError(
                f"'{agent_id}' is protected and cannot be stopped while ATLAS is running. "
                f"It is required for safe operation. Use the kill switch to halt trading."
            )
        await agent.stop()
        return agent

    async def restart_agent(self, agent_id: str) -> Agent:
        agent = self.require(agent_id)
        await agent.stop()
        await agent.start()
        return agent

    # ------------------------------------------------------------------ #
    # health
    # ------------------------------------------------------------------ #

    async def _health_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._health_interval)
                self.check_health()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("agent health check failed")

    def check_health(self) -> dict[str, str]:
        """Mark agents whose heartbeat has gone stale.

        Only applies to agents with a periodic cycle. A purely event-driven
        agent legitimately sits silent when nothing is happening — flagging it
        as unhealthy would cry wolf all weekend.
        """
        statuses: dict[str, str] = {}
        now = datetime.now(UTC)

        for agent in self._agents.values():
            statuses[agent.id] = agent.status.value

            if not agent.status.is_running:
                continue
            if agent.cycle_interval_seconds is None:
                continue

            # Allow generous headroom over the agent's own cycle time.
            timeout = max(self._heartbeat_timeout, agent.cycle_interval_seconds * 3)
            if agent.last_heartbeat is None:
                continue

            silence = (now - agent.last_heartbeat).total_seconds()
            if silence > timeout:
                agent.mark_unhealthy(f"no heartbeat for {silence:.0f}s (limit {timeout:.0f}s)")
                statuses[agent.id] = agent.status.value

        return statuses

    def health_summary(self) -> dict[str, object]:
        agents = list(self._agents.values())
        running = [a for a in agents if a.status.is_running]
        unhealthy = [a for a in agents if a.status in (AgentStatus.ERROR, AgentStatus.UNHEALTHY)]
        return {
            "registered": len(agents),
            "running": len(running),
            "stopped": len([a for a in agents if a.status is AgentStatus.STOPPED]),
            "unhealthy": len(unhealthy),
            "unhealthy_ids": [a.id for a in unhealthy],
            "all_healthy": not unhealthy,
            "protected": sorted(PROTECTED_AGENT_IDS),
        }

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def descriptors(self, log_limit: int = 20) -> list[AgentDescriptor]:
        return [a.descriptor(log_limit=log_limit) for a in self._agents.values()]

    def graph(self) -> AgentGraph:
        """Build the agent network from declared inputs and outputs.

        Derived rather than hard-coded: an edge exists because one agent
        publishes a topic another subscribes to, so the diagram cannot drift
        away from the real wiring.
        """
        nodes = [
            AgentGraphNode(id=a.id, name=a.name, type=a.agent_type, status=a.status)
            for a in self._agents.values()
        ]

        edges: list[AgentGraphEdge] = []
        for producer in self._agents.values():
            for topic in producer.outputs:
                for consumer in self._agents.values():
                    if consumer.id == producer.id:
                        continue
                    for pattern in consumer.subscriptions:
                        if topic_matches(topic, pattern):
                            edge = AgentGraphEdge(
                                source=producer.id, target=consumer.id, label=topic
                            )
                            if edge not in edges:
                                edges.append(edge)

        # The broker is not an agent but belongs on the picture: it is where
        # the pipeline ends.
        nodes.append(
            AgentGraphNode(id="broker", name="Alpaca", type="broker", status=AgentStatus.IDLE)
        )
        if "execution" in self._agents:
            edges.append(AgentGraphEdge(source="execution", target="broker", label="order.submit"))

        return AgentGraph(nodes=nodes, edges=edges)
