"""The agent framework.

An ATLAS agent is a supervised, introspectable worker with a declared
contract. Two execution shapes are supported, and an agent may use both:

*   **Periodic** — set `cycle_interval_seconds` and implement `run_cycle()`.
    Used by the scanner, the portfolio reconciler, the orchestrator.
*   **Event-driven** — declare `subscriptions` and implement `handle_event()`.
    Used by the risk, execution and mentor agents.

Every agent exposes the full descriptor the brief asked for — id, name, role,
status, current task, last activity, confidence, inputs, outputs, tools,
subscriptions, logs and statistics — so the Agents page can show not just
*that* an agent is running but *what it is doing and why*.

Failure policy: an exception inside `run_cycle()` is logged, counted, and the
loop continues after a backoff. One bad cycle (a transient API error, a symbol
with no data) must not silently kill an agent and leave the dashboard showing
a green light. Repeated failures move the agent to ERROR, which is visible.
"""

from __future__ import annotations

import abc
import asyncio
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.core.trace import get_trace_id, trace
from app.events.bus import EventBus, Subscription
from app.events.types import AgentStatusEvent, Event
from app.models.agent import AgentDescriptor, AgentLogLine, AgentStats
from app.models.enums import AgentStatus

#: Consecutive failed cycles before an agent is marked ERROR.
MAX_CONSECUTIVE_ERRORS = 5
#: Backoff applied after a failed cycle, so a persistent failure does not spin.
ERROR_BACKOFF_SECONDS = 5.0


class Agent(abc.ABC):
    """Base class for every ATLAS agent."""

    #: Matches the `type` field in agents.yaml.
    agent_type: str = "generic"
    #: Declared contract, rendered in the agent network graph.
    inputs: list[str] = []
    outputs: list[str] = []
    tools: list[str] = []
    #: Event patterns this agent consumes.
    subscriptions: list[str] = []
    #: None means "event-driven only, no periodic work".
    cycle_interval_seconds: float | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a subclass that would shadow a base-class attribute.

        `self.status` is an `AgentStatus` field set in `__init__`, so a
        subclass defining `def status(self)` would be silently replaced by the
        instance attribute — `agent.status()` then raises
        "'AgentStatus' object is not callable" at runtime, far from the cause.
        Subclass domain detail belongs in `detail()`.
        """
        super().__init_subclass__(**kwargs)

        for reserved in ("status", "current_task", "last_activity", "confidence", "stats"):
            member = cls.__dict__.get(reserved)
            if callable(member):
                raise TypeError(
                    f"{cls.__name__} defines a method '{reserved}', which collides with "
                    f"the Agent instance attribute of the same name. Use 'detail()' for "
                    f"agent-specific status information."
                )

        # Framework internals. Overriding one of these does not fail loudly —
        # it silently breaks logging or the lifecycle. The bug that motivated
        # this check: a subclass defined `async def _record(proposal, decision)`,
        # which shadowed the base `_record(level, message)`, so every
        # `self.warn(...)` in that agent created an un-awaited coroutine and
        # the log line vanished. Overridable hooks are listed below it.
        overridable = {
            "on_start",
            "on_stop",
            "run_cycle",
            "handle_event",
            "detail",
        }
        for reserved in (
            "_record",
            "_set_status",
            "_loop",
            "_sleep",
            "_on_event",
            "_clear_subscriptions",
            "start",
            "stop",
            "heartbeat",
            "set_task",
            "info",
            "warn",
            "error",
            "debug",
            "publish",
            "descriptor",
            "mark_unhealthy",
        ):
            if reserved in overridable:
                continue
            if reserved in cls.__dict__:
                raise TypeError(
                    f"{cls.__name__} overrides the Agent framework method "
                    f"'{reserved}'. Override only on_start, on_stop, run_cycle, "
                    f"handle_event or detail; pick a different name for "
                    f"agent-specific helpers."
                )

    def __init__(
        self,
        agent_id: str,
        name: str,
        role: str,
        bus: EventBus,
        config: dict[str, Any] | None = None,
        log_buffer_size: int = 200,
        autostart: bool = False,
    ) -> None:
        self.id = agent_id
        self.name = name
        self.role = role
        self.bus = bus
        self.config = config or {}
        self.autostart = autostart

        self.status = AgentStatus.CREATED
        self.current_task: str | None = None
        self.last_activity: datetime | None = None
        self.last_heartbeat: datetime | None = None
        self.confidence: float | None = None

        self.stats = AgentStats()
        self._logs: deque[AgentLogLine] = deque(maxlen=log_buffer_size)
        self._cycle_durations: deque[float] = deque(maxlen=50)

        self._task: asyncio.Task[None] | None = None
        self._subscriptions: list[Subscription] = []
        self._stop_event = asyncio.Event()
        self._consecutive_errors = 0
        #: Pause after a failed cycle, so a persistent failure (a dead API,
        #: a bad symbol) does not hammer the broker. An instance attribute
        #: rather than a constant so configuration and tests can tune it.
        self.error_backoff_seconds = ERROR_BACKOFF_SECONDS

        self.log = get_logger(f"agent.{agent_id}")

    # ------------------------------------------------------------------ #
    # lifecycle hooks for subclasses
    # ------------------------------------------------------------------ #

    async def on_start(self) -> None:
        """Called once before the loop begins. Override for setup."""

    async def on_stop(self) -> None:
        """Called once after the loop ends. Override for teardown."""

    async def run_cycle(self) -> None:
        """One unit of periodic work. Override when `cycle_interval_seconds` is set."""

    async def handle_event(self, event: Event) -> None:
        """Handle one subscribed event. Override when `subscriptions` is set."""

    def detail(self) -> dict[str, Any]:
        """Agent-specific status detail for `/api/agents/{id}`.

        Override to expose domain information (scanner rankings, risk
        counters, stream health). Deliberately *not* called `status()`, which
        is an instance attribute on this class — see `__init_subclass__`.
        """
        return {}

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Start the agent: subscribe, run on_start, launch the loop."""
        if self.status.is_running:
            self.info("start requested but agent is already running")
            return

        self._set_status(AgentStatus.STARTING, "starting up")
        self._stop_event.clear()
        self._consecutive_errors = 0
        self.stats.started_at = datetime.now(UTC)

        for pattern in self.subscriptions:
            self._subscriptions.append(
                self.bus.subscribe(pattern, self._on_event, name=f"{self.id}:{pattern}")
            )

        try:
            await self.on_start()
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_error = f"on_start failed: {exc}"
            self.log.exception("agent on_start failed", extra={"agent": self.id})
            self._set_status(AgentStatus.ERROR, f"startup failed: {exc}")
            # Leave no dangling subscriptions behind a failed start.
            await self._clear_subscriptions()
            return

        self.heartbeat()
        self._set_status(AgentStatus.IDLE, "ready")

        if self.cycle_interval_seconds is not None:
            self._task = asyncio.create_task(self._loop(), name=f"agent:{self.id}")

        self.info("started")

    async def stop(self) -> None:
        """Stop the agent and release its subscriptions."""
        if self.status in (AgentStatus.STOPPED, AgentStatus.CREATED):
            return

        self._set_status(AgentStatus.STOPPING, "shutting down")
        self._stop_event.set()

        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

        await self._clear_subscriptions()

        try:
            await self.on_stop()
        except Exception:
            self.log.exception("agent on_stop failed", extra={"agent": self.id})

        self._set_status(AgentStatus.STOPPED, "stopped")
        self.info("stopped")

    async def _clear_subscriptions(self) -> None:
        for sub in self._subscriptions:
            await self.bus.unsubscribe(sub)
        self._subscriptions.clear()

    async def _loop(self) -> None:
        """The periodic work loop."""
        interval = self.cycle_interval_seconds or 60.0
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                self._set_status(AgentStatus.WORKING, self.current_task or "working")
                with trace(prefix=self.id.replace("_", "")[:6]):
                    await self.run_cycle()

                duration_ms = (time.perf_counter() - started) * 1000
                self._cycle_durations.append(duration_ms)
                self.stats.cycles_completed += 1
                self.stats.last_cycle_duration_ms = round(duration_ms, 2)
                self.stats.average_cycle_duration_ms = round(
                    sum(self._cycle_durations) / len(self._cycle_durations), 2
                )
                self._consecutive_errors = 0
                self.heartbeat()
                self._set_status(AgentStatus.IDLE, "waiting for next cycle")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_errors += 1
                self.stats.errors += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self.error(f"cycle failed: {type(exc).__name__}: {exc}")
                self.log.exception("agent cycle failed", extra={"agent": self.id})

                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self._set_status(
                        AgentStatus.ERROR,
                        f"{self._consecutive_errors} consecutive failures",
                    )
                    # Stay alive so the operator can see the state and the
                    # error, but back right off instead of hammering.
                    await self._sleep(max(interval, 60.0))
                    continue

                await self._sleep(self.error_backoff_seconds)
                continue

            await self._sleep(interval)

    async def _sleep(self, seconds: float) -> None:
        """Interruptible sleep: a stop request wakes immediately."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _on_event(self, event: Event) -> None:
        """Bus callback wrapper: counts, times and isolates handler errors."""
        if not self.status.is_running:
            return
        try:
            self.stats.events_handled += 1
            await self.handle_event(event)
            self.heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_error = f"handle_event: {type(exc).__name__}: {exc}"
            self.error(f"event handling failed for {event.topic}: {exc}")
            self.log.exception(
                "agent event handler failed",
                extra={"agent": self.id, "topic": event.topic},
            )

    # ------------------------------------------------------------------ #
    # status / logging
    # ------------------------------------------------------------------ #

    def _set_status(self, status: AgentStatus, task: str | None = None) -> None:
        changed = status != self.status
        self.status = status
        if task is not None:
            self.current_task = task
        self.last_activity = datetime.now(UTC)

        if changed:
            self.bus.publish_nowait(
                AgentStatusEvent(
                    agent_id=self.id,
                    status=status.value,
                    current_task=self.current_task,
                    source=self.id,
                )
            )

    def set_task(self, task: str) -> None:
        """Update the one-line 'what am I doing' description."""
        self.current_task = task
        self.last_activity = datetime.now(UTC)

    def heartbeat(self) -> None:
        """Signal liveness to the AgentManager's health check."""
        now = datetime.now(UTC)
        self.last_heartbeat = now
        if self.stats.started_at:
            self.stats.uptime_seconds = (now - self.stats.started_at).total_seconds()

    def _record(self, level: str, message: str) -> None:
        # Capture the ambient trace id so an agent's own log lines can be
        # correlated with the decision chain they belong to.
        self._logs.append(AgentLogLine(level=level, message=message, trace_id=get_trace_id()))
        self.last_activity = datetime.now(UTC)

    def info(self, message: str) -> None:
        self._record("INFO", message)
        self.log.info(message, extra={"agent": self.id})

    def warn(self, message: str) -> None:
        self._record("WARNING", message)
        self.log.warning(message, extra={"agent": self.id})

    def error(self, message: str) -> None:
        self._record("ERROR", message)

    def debug(self, message: str) -> None:
        self._record("DEBUG", message)
        self.log.debug(message, extra={"agent": self.id})

    async def publish(self, event: Event) -> None:
        """Publish on the bus and count it."""
        self.stats.messages_published += 1
        await self.bus.publish(event)

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def descriptor(self, log_limit: int = 50) -> AgentDescriptor:
        return AgentDescriptor(
            id=self.id,
            name=self.name,
            role=self.role,
            type=self.agent_type,
            status=self.status,
            current_task=self.current_task,
            last_activity=self.last_activity,
            last_heartbeat=self.last_heartbeat,
            confidence=self.confidence,
            inputs=list(self.inputs),
            outputs=list(self.outputs),
            tools=list(self.tools),
            subscriptions=list(self.subscriptions),
            stats=self.stats,
            recent_logs=list(self._logs)[-log_limit:][::-1],
            autostart=self.autostart,
        )

    def mark_unhealthy(self, reason: str) -> None:
        """Called by the AgentManager when a heartbeat goes missing."""
        if self.status.is_running and self.status is not AgentStatus.UNHEALTHY:
            self.warn(f"marked unhealthy: {reason}")
            self._set_status(AgentStatus.UNHEALTHY, reason)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id} status={self.status.value}>"
