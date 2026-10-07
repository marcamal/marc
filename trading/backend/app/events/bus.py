"""The internal async event bus.

Design notes, and why it is not Kafka:

*   ATLAS runs as one process on one PC. An in-process asyncio bus has
    microsecond latency, zero operational burden, and no broker to keep alive.
    Kafka would add a dependency without adding a capability we need today.
*   The *interface* is deliberately broker-shaped (`publish` / `subscribe` on
    dotted topics with wildcards). Swapping in Redis Streams or NATS later
    means reimplementing this one class, not rewriting the agents.

**Each subscriber gets its own bounded queue and its own worker task.** This is
the important property: a slow subscriber cannot block a fast publisher. If a
subscriber falls behind, its queue fills and its *own* events are dropped, with
a counter the System page can show. Market data must never be held up because
the journal is busy writing to disk.

A handler that raises is logged and the subscription survives. One buggy agent
must not take down the bus.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence

from app.core.logging import get_logger
from app.core.trace import set_trace_id
from app.events.types import Event, topic_matches

log = get_logger(__name__)

#: A subscriber callback. Must be async.
EventHandler = Callable[[Event], Awaitable[None]]

DEFAULT_QUEUE_SIZE = 1000


class Subscription:
    """One subscriber: its patterns, its queue, its worker task."""

    def __init__(
        self,
        patterns: Sequence[str],
        handler: EventHandler,
        name: str,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.patterns = list(patterns)
        self.handler = handler
        self.name = name
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        self.task: asyncio.Task[None] | None = None
        self.delivered = 0
        self.dropped = 0
        self.errors = 0
        self.last_error: str | None = None
        self._closed = False

    def wants(self, topic: str) -> bool:
        return any(topic_matches(topic, p) for p in self.patterns)

    def offer(self, event: Event) -> bool:
        """Non-blocking enqueue. Returns False if the queue was full."""
        if self._closed:
            return False
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            # Log sparsely: a flooded subscriber would otherwise flood the log.
            if self.dropped % 100 == 1:
                log.warning(
                    "subscriber queue full, dropping events",
                    extra={
                        "subscriber": self.name,
                        "dropped_total": self.dropped,
                        "topic": event.topic,
                    },
                )
            return False

    async def _run(self) -> None:
        """Drain the queue, one event at a time, forever."""
        while True:
            event = await self.queue.get()
            try:
                # Re-establish the publisher's trace id inside the handler so
                # that logs written by the subscriber join the same chain.
                token = set_trace_id(event.trace_id)
                try:
                    await self.handler(event)
                finally:
                    try:
                        from app.core.trace import reset_trace_id

                        reset_trace_id(token)
                    except ValueError:
                        # The token belongs to a different context; harmless.
                        pass
                self.delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception(
                    "event handler failed",
                    extra={
                        "subscriber": self.name,
                        "topic": event.topic,
                        "event_id": event.id,
                    },
                )
            finally:
                self.queue.task_done()

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name=f"sub:{self.name}")

    async def stop(self) -> None:
        self._closed = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    def stats(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "patterns": self.patterns,
            "queued": self.queue.qsize(),
            "delivered": self.delivered,
            "dropped": self.dropped,
            "errors": self.errors,
            "last_error": self.last_error,
        }


class EventBus:
    """Publish/subscribe over dotted topics, in process."""

    def __init__(self, history_size: int = 500) -> None:
        self._subscriptions: dict[str, Subscription] = {}
        self._history: deque[Event] = deque(maxlen=history_size)
        self._published = 0
        self._running = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._running = True
        for sub in self._subscriptions.values():
            sub.start()
        log.info("event bus started", extra={"subscribers": len(self._subscriptions)})

    async def stop(self) -> None:
        """Stop every worker. Undelivered queued events are discarded.

        On shutdown that is the right call: finishing the queue could mean
        acting on market data from before the stop was requested.
        """
        self._running = False
        await asyncio.gather(
            *(sub.stop() for sub in self._subscriptions.values()),
            return_exceptions=True,
        )
        log.info("event bus stopped", extra={"published_total": self._published})

    # ------------------------------------------------------------------ #
    # subscribe / publish
    # ------------------------------------------------------------------ #

    def subscribe(
        self,
        patterns: str | Iterable[str],
        handler: EventHandler,
        name: str | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> Subscription:
        """Register a handler for one or more topic patterns.

        The subscription starts consuming immediately if the bus is running,
        so an agent that subscribes after startup does not silently miss
        everything.
        """
        pattern_list = [patterns] if isinstance(patterns, str) else list(patterns)
        sub = Subscription(
            patterns=pattern_list,
            handler=handler,
            name=name or getattr(handler, "__qualname__", "anonymous"),
            queue_size=queue_size,
        )
        self._subscriptions[sub.id] = sub
        if self._running:
            sub.start()
        log.debug(
            "subscription added",
            extra={"subscriber": sub.name, "patterns": pattern_list},
        )
        return sub

    async def unsubscribe(self, subscription: Subscription) -> None:
        self._subscriptions.pop(subscription.id, None)
        await subscription.stop()

    def publish_nowait(self, event: Event) -> int:
        """Fan out without awaiting delivery. Returns the subscriber count.

        This is the hot path for market data: the websocket reader calls it and
        returns to reading immediately.
        """
        self._published += 1
        self._history.append(event)

        matched = 0
        for sub in self._subscriptions.values():
            if sub.wants(event.topic):
                matched += 1
                sub.offer(event)
        return matched

    async def publish(self, event: Event) -> int:
        """Async alias for `publish_nowait`.

        Kept async so that call sites read naturally and so a future
        network-backed bus can become genuinely awaitable without touching
        every publisher.
        """
        return self.publish_nowait(event)

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def history(self, limit: int = 100, pattern: str = "*") -> list[Event]:
        """Recent events, newest first. Powers the System events panel."""
        selected = [e for e in self._history if topic_matches(e.topic, pattern)]
        return list(reversed(selected[-limit:]))

    def stats(self) -> dict[str, object]:
        return {
            "running": self._running,
            "published_total": self._published,
            "history_size": len(self._history),
            "subscribers": [s.stats() for s in self._subscriptions.values()],
            "total_dropped": sum(s.dropped for s in self._subscriptions.values()),
        }

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)


async def wait_for_event(
    bus: EventBus,
    pattern: str,
    timeout: float = 5.0,
    predicate: Callable[[Event], bool] | None = None,
) -> Event | None:
    """Wait for the next matching event. Test helper, not for production code.

    Returns None on timeout rather than raising, which keeps tests readable.
    """
    future: asyncio.Future[Event] = asyncio.get_running_loop().create_future()

    async def _capture(event: Event) -> None:
        if not future.done() and (predicate is None or predicate(event)):
            future.set_result(event)

    sub = bus.subscribe(pattern, _capture, name="wait_for_event")
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except TimeoutError:
        return None
    finally:
        await bus.unsubscribe(sub)
