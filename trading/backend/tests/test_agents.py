"""Agent framework tests: lifecycle, error isolation, health, protection."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.base import MAX_CONSECUTIVE_ERRORS, Agent
from app.agents.manager import PROTECTED_AGENT_IDS, AgentManager, AgentManagerError
from app.events.bus import EventBus
from app.events.types import Event
from app.models.enums import AgentStatus
from app.runtime import AtlasRuntime


class CountingAgent(Agent):
    """A periodic agent that counts its cycles."""

    agent_type = "test_counter"
    outputs = ["test.output"]
    cycle_interval_seconds = 0.05

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cycles = 0
        self.started = False
        self.stopped = False

    async def on_start(self) -> None:
        self.started = True

    async def on_stop(self) -> None:
        self.stopped = True

    async def run_cycle(self) -> None:
        self.cycles += 1

    def detail(self) -> dict[str, Any]:
        return {"cycles": self.cycles}


class ListeningAgent(Agent):
    """An event-driven agent."""

    agent_type = "test_listener"
    subscriptions = ["test.output"]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.events: list[Event] = []

    async def handle_event(self, event: Event) -> None:
        self.events.append(event)


class FailingAgent(Agent):
    agent_type = "test_failing"
    cycle_interval_seconds = 0.01

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.attempts = 0

    async def run_cycle(self) -> None:
        self.attempts += 1
        raise RuntimeError("deliberate cycle failure")


class FailingStartAgent(Agent):
    agent_type = "test_failing_start"
    subscriptions = ["test.output"]

    async def on_start(self) -> None:
        raise RuntimeError("deliberate startup failure")


class StallingAgent(Agent):
    """A periodic agent whose cycle hangs, so it stops heartbeating.

    This is the failure the health monitor exists to catch: the agent looks
    "running" while actually being stuck on a call that never returns.
    """

    agent_type = "test_stalling"
    cycle_interval_seconds = 0.01

    async def run_cycle(self) -> None:
        await asyncio.sleep(30)


def _make(cls: type[Agent], bus: EventBus, agent_id: str = "test") -> Agent:
    return cls(
        agent_id=agent_id,
        name=f"Test {agent_id}",
        role="a test agent",
        bus=bus,
        config={},
    )


# --------------------------------------------------------------------------- #
# the framework guards against name collisions
# --------------------------------------------------------------------------- #


def test_subclass_cannot_shadow_the_status_attribute() -> None:
    """`self.status` is an AgentStatus, so a `status()` method breaks silently."""
    with pytest.raises(TypeError, match="collides with the Agent instance attribute"):

        class BadAgent(Agent):
            def status(self) -> dict[str, Any]:  # type: ignore[override]
                return {}


def test_subclass_cannot_override_framework_internals() -> None:
    """Overriding `_record` silently broke logging once; now it fails loudly."""
    with pytest.raises(TypeError, match="overrides the Agent framework method"):

        class BadAgent(Agent):
            async def _record(self, a: Any, b: Any) -> None:  # type: ignore[override]
                return None


def test_subclass_may_override_the_documented_hooks(bus: EventBus) -> None:
    """The five intended extension points must remain overridable."""

    class GoodAgent(Agent):
        async def on_start(self) -> None: ...
        async def on_stop(self) -> None: ...
        async def run_cycle(self) -> None: ...
        async def handle_event(self, event: Event) -> None: ...
        def detail(self) -> dict[str, Any]:
            return {}

    assert _make(GoodAgent, bus).id == "test"


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #


async def test_agent_starts_and_runs_cycles(started_bus: EventBus) -> None:
    agent = _make(CountingAgent, started_bus)
    assert agent.status is AgentStatus.CREATED

    await agent.start()
    assert agent.started is True
    assert agent.status.is_running
    assert agent.last_heartbeat is not None

    await asyncio.sleep(0.2)
    assert agent.cycles >= 2, "the periodic loop should have run"
    assert agent.stats.cycles_completed >= 2
    assert agent.stats.last_cycle_duration_ms is not None

    await agent.stop()
    assert agent.stopped is True
    assert agent.status is AgentStatus.STOPPED


async def test_stopping_cancels_the_loop(started_bus: EventBus) -> None:
    agent = _make(CountingAgent, started_bus)
    await agent.start()
    await asyncio.sleep(0.12)
    await agent.stop()

    count = agent.cycles  # type: ignore[attr-defined]
    await asyncio.sleep(0.15)
    assert agent.cycles == count, "no cycles should run after stop"  # type: ignore[attr-defined]


async def test_double_start_is_a_noop(started_bus: EventBus) -> None:
    agent = _make(CountingAgent, started_bus)
    await agent.start()
    await agent.start()
    await asyncio.sleep(0.05)
    await agent.stop()
    # One loop task, not two: the cycle count stays plausible.
    assert agent.stats.errors == 0


async def test_stop_before_start_is_safe(started_bus: EventBus) -> None:
    agent = _make(CountingAgent, started_bus)
    await agent.stop()
    assert agent.status is AgentStatus.CREATED


async def test_subscriptions_released_on_stop(started_bus: EventBus) -> None:
    agent = _make(ListeningAgent, started_bus)
    before = started_bus.subscriber_count

    await agent.start()
    assert started_bus.subscriber_count == before + 1

    await agent.stop()
    assert started_bus.subscriber_count == before, "stopping must release subscriptions"


async def test_failed_start_leaves_no_dangling_subscriptions(
    started_bus: EventBus,
) -> None:
    agent = _make(FailingStartAgent, started_bus)
    before = started_bus.subscriber_count

    await agent.start()

    assert agent.status is AgentStatus.ERROR
    assert started_bus.subscriber_count == before, (
        "a failed start must not leave subscriptions behind"
    )


# --------------------------------------------------------------------------- #
# event handling
# --------------------------------------------------------------------------- #


async def test_event_driven_agent_receives_events(started_bus: EventBus) -> None:
    agent = _make(ListeningAgent, started_bus)
    await agent.start()

    await started_bus.publish(Event(topic="test.output"))
    await asyncio.sleep(0.05)

    assert len(agent.events) == 1  # type: ignore[attr-defined]
    assert agent.stats.events_handled == 1
    await agent.stop()


async def test_stopped_agent_ignores_events(started_bus: EventBus) -> None:
    agent = _make(ListeningAgent, started_bus)
    await agent.start()
    await agent.stop()

    await started_bus.publish(Event(topic="test.output"))
    await asyncio.sleep(0.05)

    assert agent.events == []  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# error handling
# --------------------------------------------------------------------------- #


async def test_cycle_failure_does_not_kill_the_agent(started_bus: EventBus) -> None:
    """A transient API error must not silently stop an agent."""
    agent = _make(FailingAgent, started_bus)
    await agent.start()
    await asyncio.sleep(0.1)

    assert agent.stats.errors > 0
    assert agent.stats.last_error is not None
    assert agent.status.is_running or agent.status is AgentStatus.ERROR
    await agent.stop()


async def test_repeated_failures_mark_the_agent_in_error(started_bus: EventBus) -> None:
    """Persistent failure must become visible, not be retried forever quietly."""
    agent = _make(FailingAgent, started_bus)
    agent.error_backoff_seconds = 0.0  # the real 5s backoff would stall the test
    await agent.start()

    for _ in range(200):
        await asyncio.sleep(0.01)
        if agent.status is AgentStatus.ERROR:
            break

    assert agent.status is AgentStatus.ERROR
    assert agent.stats.errors >= MAX_CONSECUTIVE_ERRORS
    await agent.stop()


async def test_error_backoff_is_applied_between_failures(started_bus: EventBus) -> None:
    """A failing agent must pause, not spin at full speed against a dead API."""
    agent = _make(FailingAgent, started_bus)
    agent.error_backoff_seconds = 0.5
    await agent.start()
    await asyncio.sleep(0.15)

    assert agent.attempts == 1, (  # type: ignore[attr-defined]
        "the backoff should prevent a second attempt this soon"
    )
    await agent.stop()


# --------------------------------------------------------------------------- #
# introspection
# --------------------------------------------------------------------------- #


async def test_descriptor_exposes_the_full_contract(started_bus: EventBus) -> None:
    """The brief listed exactly what the Agents page must show."""
    agent = _make(CountingAgent, started_bus)
    await agent.start()
    agent.info("a log line")
    await asyncio.sleep(0.08)

    descriptor = agent.descriptor()
    assert descriptor.id == "test"
    assert descriptor.name
    assert descriptor.role
    assert descriptor.type == "test_counter"
    assert descriptor.status.is_running
    assert descriptor.last_activity is not None
    assert descriptor.last_heartbeat is not None
    assert descriptor.outputs == ["test.output"]
    assert descriptor.stats.cycles_completed >= 1
    assert any(line.message == "a log line" for line in descriptor.recent_logs)

    await agent.stop()


async def test_log_buffer_is_bounded(started_bus: EventBus) -> None:
    agent = CountingAgent(
        agent_id="t", name="t", role="t", bus=started_bus, config={}, log_buffer_size=10
    )
    for i in range(50):
        agent.info(f"line {i}")

    assert len(agent.descriptor(log_limit=100).recent_logs) == 10


async def test_default_detail_is_empty(started_bus: EventBus) -> None:
    agent = _make(ListeningAgent, started_bus)
    assert agent.detail() == {}


# --------------------------------------------------------------------------- #
# the manager
# --------------------------------------------------------------------------- #


async def test_manager_registers_and_starts(started_bus: EventBus) -> None:
    manager = AgentManager(started_bus, health_check_interval_seconds=10)
    agent = _make(CountingAgent, started_bus, "counter")
    agent.autostart = True
    manager.register(agent)

    await manager.start_all()
    try:
        assert manager.is_running("counter")
        assert manager.health_summary()["running"] == 1
    finally:
        await manager.stop_all()

    assert agent.status is AgentStatus.STOPPED


async def test_manager_rejects_duplicate_ids(started_bus: EventBus) -> None:
    manager = AgentManager(started_bus)
    manager.register(_make(CountingAgent, started_bus, "dup"))

    with pytest.raises(AgentManagerError, match="already registered"):
        manager.register(_make(CountingAgent, started_bus, "dup"))


async def test_manager_requires_a_known_agent(started_bus: EventBus) -> None:
    manager = AgentManager(started_bus)
    with pytest.raises(AgentManagerError, match="no agent registered"):
        manager.require("nope")


async def test_autostart_false_agents_stay_stopped(started_bus: EventBus) -> None:
    manager = AgentManager(started_bus, health_check_interval_seconds=10)
    agent = _make(CountingAgent, started_bus, "manual")
    agent.autostart = False
    manager.register(agent)

    await manager.start_all()
    try:
        assert not manager.is_running("manual")
    finally:
        await manager.stop_all()


async def test_health_check_flags_a_stalled_agent(started_bus: EventBus) -> None:
    """A stuck agent must be flagged, not left showing a green light."""
    manager = AgentManager(
        started_bus, health_check_interval_seconds=10, heartbeat_timeout_seconds=0.01
    )
    agent = _make(StallingAgent, started_bus, "staller")
    manager.register(agent)
    await agent.start()

    await asyncio.sleep(0.1)
    manager.check_health()

    assert agent.status is AgentStatus.UNHEALTHY
    assert "staller" in manager.health_summary()["unhealthy_ids"]  # type: ignore[operator]
    await agent.stop()


async def test_health_check_leaves_a_healthy_agent_alone(started_bus: EventBus) -> None:
    """An agent that heartbeats every cycle must not be flagged."""
    manager = AgentManager(
        started_bus, health_check_interval_seconds=10, heartbeat_timeout_seconds=5.0
    )
    agent = _make(CountingAgent, started_bus, "counter")
    manager.register(agent)
    await agent.start()

    await asyncio.sleep(0.1)
    manager.check_health()

    assert agent.status is not AgentStatus.UNHEALTHY
    assert manager.health_summary()["all_healthy"] is True
    await agent.stop()


async def test_health_check_ignores_event_driven_agents(started_bus: EventBus) -> None:
    """An idle event-driven agent is not unhealthy — it is just quiet."""
    manager = AgentManager(
        started_bus, health_check_interval_seconds=10, heartbeat_timeout_seconds=0.01
    )
    agent = _make(ListeningAgent, started_bus, "listener")
    manager.register(agent)
    await agent.start()

    await asyncio.sleep(0.05)
    manager.check_health()

    assert agent.status is not AgentStatus.UNHEALTHY
    await agent.stop()


# --------------------------------------------------------------------------- #
# protected agents
# --------------------------------------------------------------------------- #


async def test_risk_and_execution_are_protected(runtime: AtlasRuntime) -> None:
    """Stopping the Risk Agent would leave execution without a veto."""
    assert {"risk", "execution"} == PROTECTED_AGENT_IDS

    for agent_id in PROTECTED_AGENT_IDS:
        with pytest.raises(AgentManagerError, match="protected"):
            await runtime.agents.stop_agent(agent_id)
        assert runtime.agents.is_running(agent_id), f"{agent_id} must still be running"


async def test_protected_agents_can_be_stopped_on_shutdown(runtime: AtlasRuntime) -> None:
    """`force=True` exists only for full shutdown, and is not exposed over HTTP."""
    await runtime.agents.stop_agent("risk", force=True)
    assert not runtime.agents.is_running("risk")


async def test_unprotected_agents_can_be_stopped(runtime: AtlasRuntime) -> None:
    await runtime.agents.start_agent("scanner")
    assert runtime.agents.is_running("scanner")

    await runtime.agents.stop_agent("scanner")
    assert not runtime.agents.is_running("scanner")


# --------------------------------------------------------------------------- #
# the agent graph
# --------------------------------------------------------------------------- #


async def test_graph_is_derived_from_declared_topics(runtime: AtlasRuntime) -> None:
    """Edges come from real wiring, so the diagram cannot drift from the code."""
    graph = runtime.agents.graph()
    node_ids = {node.id for node in graph.nodes}

    for expected in ("orchestrator", "market_data", "risk", "execution", "broker"):
        assert expected in node_ids

    # The decisive edge in the whole system: risk feeds execution.
    assert any(edge.source == "risk" and edge.target == "execution" for edge in graph.edges), (
        "risk.decision must flow to the execution agent"
    )

    # And execution is the only thing that reaches the broker.
    broker_edges = [e for e in graph.edges if e.target == "broker"]
    assert broker_edges and all(e.source == "execution" for e in broker_edges)


async def test_all_configured_agents_are_built(runtime: AtlasRuntime) -> None:
    expected = {entry.id for entry in runtime.config.agents.agents}
    actual = {agent.id for agent in runtime.agents.agents}
    assert expected == actual


async def test_autostart_agents_are_running(runtime: AtlasRuntime) -> None:
    for entry in runtime.config.agents.agents:
        if entry.autostart:
            assert runtime.agents.is_running(entry.id), f"{entry.id} should have started"
