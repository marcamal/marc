"""WebSocket endpoint for the dashboard.

One socket per browser tab, fed from the internal event bus. The browser never
connects to Alpaca — it connects here, and ATLAS relays normalised events. That
keeps brokerage credentials entirely server-side.

Backpressure: each client gets a bounded queue. A browser tab that cannot keep
up (minimised, throttled, slow machine) drops its own oldest events rather than
slowing the bus down for everything else.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.events.types import Event

router = APIRouter()
log = get_logger(__name__)

#: Topics pushed to the UI. Quotes and trades are excluded on purpose: at tick
#: rate they would flood the socket for data the dashboard polls anyway.
UI_TOPICS = [
    "market.bar",
    "market.clock",
    "market.stale",
    "connection.*",
    "scanner.results",
    "signal.*",
    "proposal.created",
    "risk.*",
    "execution.*",
    "order.update",
    "account.update",
    "portfolio.snapshot",
    "agent.status",
    "system.*",
    "mentor.explanation",
]

CLIENT_QUEUE_SIZE = 200


@router.websocket("/ws/events")
async def events_socket(websocket: WebSocket) -> None:
    """Stream live events to the dashboard."""
    runtime: Any = getattr(websocket.app.state, "runtime", None)
    if runtime is None:
        await websocket.close(code=1013, reason="ATLAS is still starting")
        return

    await websocket.accept()

    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
    dropped = 0

    async def forward(event: Event) -> None:
        nonlocal dropped
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest so the client gets current state rather than a
            # stalled socket full of history.
            dropped += 1
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
                queue.task_done()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    subscription = runtime.bus.subscribe(UI_TOPICS, forward, name="websocket:ui")

    try:
        # Send the current state immediately, so the UI paints without waiting
        # for the first event to happen to arrive.
        await websocket.send_json({"type": "snapshot", "data": runtime.status()})

        while True:
            event = await queue.get()
            try:
                await websocket.send_json(
                    {
                        "type": "event",
                        "topic": event.topic,
                        "data": event.model_dump(mode="json"),
                    }
                )
            finally:
                queue.task_done()

    except WebSocketDisconnect:
        log.debug("dashboard websocket disconnected")
    except (asyncio.CancelledError, RuntimeError):
        # Normal on shutdown, or when the client vanishes mid-send.
        pass
    except Exception:
        log.exception("dashboard websocket error")
    finally:
        await runtime.bus.unsubscribe(subscription)
        if dropped:
            log.debug("dashboard websocket dropped %d events", dropped)
        with contextlib.suppress(Exception):
            await websocket.close()
