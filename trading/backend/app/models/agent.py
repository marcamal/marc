"""Agent introspection models — what the Agents page renders."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, computed_field

from app.models.enums import AgentStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentLogLine(BaseModel):
    timestamp: datetime = Field(default_factory=_utcnow)
    level: str = "INFO"
    message: str
    trace_id: str | None = None


class AgentStats(BaseModel):
    """Cumulative counters, reset only on process restart."""

    cycles_completed: int = 0
    events_handled: int = 0
    messages_published: int = 0
    errors: int = 0
    last_error: str | None = None
    last_cycle_duration_ms: float | None = None
    average_cycle_duration_ms: float | None = None
    started_at: datetime | None = None
    #: Total seconds the agent has been in a running state.
    uptime_seconds: float = 0.0


class AgentDescriptor(BaseModel):
    """Everything the dashboard needs to show one agent.

    The brief asked for: unique id, name, role, status, current task, last
    activity, confidence, inputs, outputs, tools, event subscriptions, logs
    and performance statistics. All of it is here.
    """

    id: str
    name: str
    role: str
    type: str
    status: AgentStatus = AgentStatus.CREATED
    #: One short line: what this agent is doing *right now*.
    current_task: str | None = None
    last_activity: datetime | None = None
    last_heartbeat: datetime | None = None
    #: The agent's own confidence in its most recent output, 0-1.
    confidence: float | None = None

    #: Declared data dependencies and products, for the network graph.
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    subscriptions: list[str] = Field(default_factory=list)

    stats: AgentStats = Field(default_factory=AgentStats)
    recent_logs: list[AgentLogLine] = Field(default_factory=list)
    autostart: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_healthy(self) -> bool:
        return self.status not in (AgentStatus.ERROR, AgentStatus.UNHEALTHY)


class AgentGraphNode(BaseModel):
    id: str
    name: str
    type: str
    status: AgentStatus


class AgentGraphEdge(BaseModel):
    source: str
    target: str
    #: The event topic that flows along this edge.
    label: str


class AgentGraph(BaseModel):
    """The ORCHESTRATOR -> DATA -> SCANNER -> ... -> ALPACA network.

    Derived from the agents' declared subscriptions and outputs rather than
    hard-coded, so the picture cannot drift from the wiring.
    """

    nodes: list[AgentGraphNode] = Field(default_factory=list)
    edges: list[AgentGraphEdge] = Field(default_factory=list)
