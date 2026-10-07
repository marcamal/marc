"""Kill switch tests.

The kill switch is the operator's last line of defence, so its behaviour is
pinned down precisely: file-based *and* in-memory, idempotent, and defaulting
to never selling anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.brokers.base import OrderRequest
from app.brokers.simulated import SimulatedBroker
from app.config.schema import KillSwitchConfig
from app.events.bus import EventBus
from app.risk.kill_switch import KillSwitch


@pytest.fixture
def switch(tmp_path: Path, bus: EventBus) -> KillSwitch:
    return KillSwitch(
        config=KillSwitchConfig(
            file_path="data/KILL_SWITCH",
            cancel_open_orders=True,
            flatten_positions=False,
            # Deliberately mixed: one enabled, one disabled, so both the
            # "fires" and the "respects the off switch" paths are covered.
            auto_triggers={
                "daily_loss_breached": True,
                "repeated_order_rejections": True,
                "data_feed_dead": False,
            },
            repeated_rejection_threshold=3,
        ),
        bus=bus,
        project_root=tmp_path,
    )


# --------------------------------------------------------------------------- #
# basic state
# --------------------------------------------------------------------------- #


def test_starts_disengaged(switch: KillSwitch) -> None:
    assert switch.is_engaged is False
    assert switch.file_present is False


async def test_engage_blocks_and_persists(switch: KillSwitch) -> None:
    await switch.engage("test reason", triggered_by="unit_test")

    assert switch.is_engaged is True
    assert switch.reason == "test reason"
    assert switch.triggered_by == "unit_test"
    assert switch.engaged_at is not None

    # The file must exist so a restart does not silently resume trading.
    assert switch.file_present is True
    content = switch.file_path.read_text(encoding="utf-8")
    assert "test reason" in content
    assert "Delete this file" in content


async def test_release_clears_flag_and_file(switch: KillSwitch) -> None:
    await switch.engage("x")
    await switch.release()

    assert switch.is_engaged is False
    assert switch.file_present is False
    assert switch.reason is None


async def test_file_alone_engages_the_switch(switch: KillSwitch) -> None:
    """Creating the file by hand must stop trading, with no API involved.

    This is the property that makes the kill switch trustworthy: it works when
    the software is broken.
    """
    assert switch.is_engaged is False
    switch.file_path.parent.mkdir(parents=True, exist_ok=True)
    switch.file_path.write_text("stop", encoding="utf-8")

    assert switch.is_engaged is True, "the file alone must engage the switch"


async def test_file_state_is_not_cached(switch: KillSwitch) -> None:
    """Checked live every time, so an external change takes effect at once."""
    switch.file_path.parent.mkdir(parents=True, exist_ok=True)
    switch.file_path.write_text("stop", encoding="utf-8")
    assert switch.is_engaged is True

    switch.file_path.unlink()
    assert switch.is_engaged is False


async def test_engage_is_idempotent(switch: KillSwitch, broker: SimulatedBroker) -> None:
    """Engaging twice must not cancel or flatten a second time."""
    await broker.submit_order(
        OrderRequest(
            symbol="SPY",
            side="buy",
            quantity=1,
            order_type="limit",
            limit_price=1.0,
            client_order_id="resting-1",
        )
    )

    first = await switch.engage("first", broker=broker)
    second = await switch.engage("second", broker=broker)

    assert first.orders_canceled == 1
    assert second.orders_canceled == 0, "a repeat engage must not act again"
    assert switch.reason == "second"


# --------------------------------------------------------------------------- #
# side effects
# --------------------------------------------------------------------------- #


async def test_engage_cancels_resting_orders(switch: KillSwitch, broker: SimulatedBroker) -> None:
    for i in range(3):
        await broker.submit_order(
            OrderRequest(
                symbol="SPY",
                side="buy",
                quantity=1,
                order_type="limit",
                limit_price=1.0,
                client_order_id=f"resting-{i}",
            )
        )
    assert len(await broker.get_orders(status="open")) == 3

    event = await switch.engage("cancel test", broker=broker)

    assert event.orders_canceled == 3
    assert await broker.get_orders(status="open") == []


async def test_engage_does_not_flatten_by_default(
    switch: KillSwitch, broker: SimulatedBroker
) -> None:
    """Flattening must be opt-in.

    Market-selling the whole book during a data outage or a flash dislocation
    converts a paper problem into a realised loss.
    """
    await broker.submit_order(
        OrderRequest(symbol="SPY", side="buy", quantity=2, client_order_id="pos-1")
    )
    assert len(await broker.get_positions()) == 1

    event = await switch.engage("no flatten", broker=broker)

    assert event.positions_flattened == 0
    assert len(await broker.get_positions()) == 1, "positions must be left alone"


async def test_engage_flattens_when_explicitly_configured(
    tmp_path: Path, bus: EventBus, broker: SimulatedBroker
) -> None:
    switch = KillSwitch(
        config=KillSwitchConfig(flatten_positions=True, cancel_open_orders=True),
        bus=bus,
        project_root=tmp_path,
    )
    await broker.submit_order(
        OrderRequest(symbol="SPY", side="buy", quantity=2, client_order_id="pos-1")
    )

    event = await switch.engage("flatten please", broker=broker)

    assert event.positions_flattened == 1
    assert await broker.get_positions() == []


async def test_engage_survives_a_broker_failure(switch: KillSwitch) -> None:
    """Being unable to cancel must not stop the switch from blocking."""

    class BrokenBroker(SimulatedBroker):
        async def cancel_all_orders(self) -> int:
            from app.brokers.base import BrokerConnectionError

            raise BrokerConnectionError("broker is down")

    await switch.engage("broker down", broker=BrokenBroker())

    assert switch.is_engaged is True, "blocking must not depend on the broker"


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #


async def test_engage_publishes_an_event(started_bus: EventBus, tmp_path: Path) -> None:
    import asyncio

    from app.events.types import KillSwitchEvent, Topics

    received: list[KillSwitchEvent] = []

    async def handler(event: object) -> None:
        received.append(event)  # type: ignore[arg-type]

    started_bus.subscribe(Topics.KILL_SWITCH, handler, name="t")
    switch = KillSwitch(config=KillSwitchConfig(), bus=started_bus, project_root=tmp_path)

    await switch.engage("event test")
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0].engaged is True


# --------------------------------------------------------------------------- #
# automatic triggers
# --------------------------------------------------------------------------- #


async def test_enabled_auto_trigger_engages(switch: KillSwitch) -> None:
    engaged = await switch.check_auto_trigger("daily_loss_breached", "down 3%")

    assert engaged is True
    assert switch.is_engaged is True
    assert switch.triggered_by == "auto:daily_loss_breached"


async def test_disabled_auto_trigger_does_not_engage(switch: KillSwitch) -> None:
    """A disabled trigger must be respected, but still logged."""
    engaged = await switch.check_auto_trigger("data_feed_dead", "feed silent")

    assert engaged is False
    assert switch.is_engaged is False


async def test_unknown_auto_trigger_does_not_engage(switch: KillSwitch) -> None:
    """Fail safe on an unrecognised trigger name: do not fire unexpectedly."""
    assert await switch.check_auto_trigger("not_a_real_trigger", "x") is False


async def test_rejection_streak_engages_at_the_threshold(switch: KillSwitch) -> None:
    """Repeated rejections mean something systematic is wrong."""
    assert await switch.record_order_rejection() is False  # 1
    assert await switch.record_order_rejection() is False  # 2
    assert switch.is_engaged is False

    assert await switch.record_order_rejection() is True  # 3 == threshold
    assert switch.is_engaged is True


async def test_success_resets_the_rejection_streak(switch: KillSwitch) -> None:
    await switch.record_order_rejection()
    await switch.record_order_rejection()
    switch.record_order_success()

    assert await switch.record_order_rejection() is False
    assert switch.is_engaged is False


async def test_status_payload(switch: KillSwitch) -> None:
    await switch.engage("status test", triggered_by="unit")
    status = switch.status()

    assert status["engaged"] is True
    assert status["reason"] == "status test"
    assert status["triggered_by"] == "unit"
    assert status["will_flatten_positions"] is False
    assert "file_path" in status
