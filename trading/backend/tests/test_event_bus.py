"""Event bus tests: delivery, wildcards, isolation and backpressure."""

from __future__ import annotations

import asyncio

import pytest

from app.events.bus import EventBus, wait_for_event
from app.events.types import Event, Topics, topic_matches

# --------------------------------------------------------------------------- #
# topic matching
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("topic", "pattern", "expected"),
    [
        ("market.bar", "market.bar", True),
        ("market.bar", "market.*", True),
        ("market.bar", "*", True),
        ("market.bar", "risk.*", False),
        ("market.bar", "market.quote", False),
        ("risk.decision", "risk.*", True),
        ("system.kill_switch", "system.*", True),
        ("market", "market.*", False),
    ],
)
def test_topic_matching(topic: str, pattern: str, expected: bool) -> None:
    assert topic_matches(topic, pattern) is expected


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #


async def test_event_delivered_to_subscriber(started_bus: EventBus) -> None:
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    started_bus.subscribe("test.topic", handler, name="t")
    await started_bus.publish(Event(topic="test.topic"))
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0].topic == "test.topic"


async def test_wildcard_subscription(started_bus: EventBus) -> None:
    received: list[str] = []

    async def handler(event: Event) -> None:
        received.append(event.topic)

    started_bus.subscribe("market.*", handler, name="t")
    for topic in ("market.bar", "market.quote", "risk.decision"):
        await started_bus.publish(Event(topic=topic))
    await asyncio.sleep(0.05)

    assert sorted(received) == ["market.bar", "market.quote"]


async def test_multiple_patterns_in_one_subscription(started_bus: EventBus) -> None:
    received: list[str] = []

    async def handler(event: Event) -> None:
        received.append(event.topic)

    started_bus.subscribe(["market.bar", "risk.*"], handler, name="t")
    for topic in ("market.bar", "risk.decision", "market.quote"):
        await started_bus.publish(Event(topic=topic))
    await asyncio.sleep(0.05)

    assert sorted(received) == ["market.bar", "risk.decision"]


async def test_one_event_reaches_every_matching_subscriber(started_bus: EventBus) -> None:
    counts = {"a": 0, "b": 0}

    async def make(key: str):
        async def handler(_: Event) -> None:
            counts[key] += 1

        return handler

    started_bus.subscribe("x.y", await make("a"), name="a")
    started_bus.subscribe("x.*", await make("b"), name="b")
    await started_bus.publish(Event(topic="x.y"))
    await asyncio.sleep(0.05)

    assert counts == {"a": 1, "b": 1}


async def test_unsubscribe_stops_delivery(started_bus: EventBus) -> None:
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    sub = started_bus.subscribe("test.*", handler, name="t")
    await started_bus.publish(Event(topic="test.one"))
    await asyncio.sleep(0.05)
    await started_bus.unsubscribe(sub)
    await started_bus.publish(Event(topic="test.two"))
    await asyncio.sleep(0.05)

    assert len(received) == 1


# --------------------------------------------------------------------------- #
# isolation: one bad subscriber must not affect others
# --------------------------------------------------------------------------- #


async def test_failing_handler_does_not_break_the_bus(started_bus: EventBus) -> None:
    """A buggy agent must not take down the event system."""
    good_received: list[Event] = []

    async def broken(_: Event) -> None:
        raise RuntimeError("deliberate failure")

    async def good(event: Event) -> None:
        good_received.append(event)

    broken_sub = started_bus.subscribe("test.*", broken, name="broken")
    started_bus.subscribe("test.*", good, name="good")

    for _ in range(3):
        await started_bus.publish(Event(topic="test.x"))
    await asyncio.sleep(0.1)

    assert len(good_received) == 3, "a failing subscriber must not block a healthy one"
    assert broken_sub.errors == 3
    assert broken_sub.last_error is not None
    # The subscription survives its own errors and keeps consuming.
    assert broken_sub.task is not None and not broken_sub.task.done()


async def test_slow_subscriber_does_not_block_publisher(started_bus: EventBus) -> None:
    """Market data must never wait on a slow consumer."""
    fast_received: list[Event] = []

    async def slow(_: Event) -> None:
        await asyncio.sleep(0.5)

    async def fast(event: Event) -> None:
        fast_received.append(event)

    started_bus.subscribe("test.*", slow, name="slow")
    started_bus.subscribe("test.*", fast, name="fast")

    for _ in range(5):
        await started_bus.publish(Event(topic="test.x"))
    await asyncio.sleep(0.1)

    # The slow handler is still on its first event; the fast one has them all.
    assert len(fast_received) == 5


async def test_full_queue_drops_rather_than_blocking(started_bus: EventBus) -> None:
    """Backpressure: a subscriber that cannot keep up drops its own events."""
    gate = asyncio.Event()

    async def blocked(_: Event) -> None:
        await gate.wait()

    sub = started_bus.subscribe("test.*", blocked, name="blocked", queue_size=3)

    for _ in range(20):
        await started_bus.publish(Event(topic="test.x"))
    await asyncio.sleep(0.05)

    assert sub.dropped > 0, "a full queue should drop events, not block the publisher"
    gate.set()


# --------------------------------------------------------------------------- #
# trace propagation
# --------------------------------------------------------------------------- #


async def test_trace_id_is_restored_inside_the_handler(started_bus: EventBus) -> None:
    """A subscriber's logs must join the publisher's decision chain."""
    from app.core.trace import get_trace_id

    seen: list[str | None] = []

    async def handler(_: Event) -> None:
        seen.append(get_trace_id())

    started_bus.subscribe("test.*", handler, name="t")
    await started_bus.publish(Event(topic="test.x", trace_id="trace-abc"))
    await asyncio.sleep(0.05)

    assert seen == ["trace-abc"]


# --------------------------------------------------------------------------- #
# history and stats
# --------------------------------------------------------------------------- #


async def test_history_is_newest_first(started_bus: EventBus) -> None:
    for topic in ("a.1", "a.2", "a.3"):
        await started_bus.publish(Event(topic=topic))

    history = started_bus.history(limit=10)
    assert [e.topic for e in history] == ["a.3", "a.2", "a.1"]


async def test_history_filtered_by_pattern(started_bus: EventBus) -> None:
    await started_bus.publish(Event(topic="market.bar"))
    await started_bus.publish(Event(topic="risk.decision"))

    assert [e.topic for e in started_bus.history(pattern="risk.*")] == ["risk.decision"]


async def test_history_is_bounded() -> None:
    bus = EventBus(history_size=5)
    await bus.start()
    try:
        for i in range(20):
            await bus.publish(Event(topic=f"t.{i}"))
        assert len(bus.history(limit=100)) == 5
    finally:
        await bus.stop()


async def test_stats_report_counts(started_bus: EventBus) -> None:
    async def handler(_: Event) -> None:
        return None

    started_bus.subscribe("test.*", handler, name="t")
    await started_bus.publish(Event(topic="test.x"))
    await asyncio.sleep(0.05)

    stats = started_bus.stats()
    assert stats["running"] is True
    assert stats["published_total"] == 1
    assert stats["subscribers"][0]["delivered"] == 1


async def test_subscription_added_after_start_still_receives(started_bus: EventBus) -> None:
    """An agent that starts late must not silently miss everything."""
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    started_bus.subscribe("late.*", handler, name="late")
    await started_bus.publish(Event(topic="late.x"))
    await asyncio.sleep(0.05)

    assert len(received) == 1


async def test_wait_for_event_helper(started_bus: EventBus) -> None:
    async def publish_soon() -> None:
        await asyncio.sleep(0.02)
        await started_bus.publish(Event(topic=Topics.SYSTEM_STARTED))

    # Keep a reference: a bare create_task() may be garbage collected
    # before it runs.
    task = asyncio.create_task(publish_soon())
    event = await wait_for_event(started_bus, Topics.SYSTEM_STARTED, timeout=1.0)
    await task
    assert event is not None


async def test_wait_for_event_times_out(started_bus: EventBus) -> None:
    assert await wait_for_event(started_bus, "never.happens", timeout=0.05) is None
