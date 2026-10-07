"""Market Data Agent — owns the streaming connection and data freshness.

A thin supervisor over `MarketDataService`. The service does the work; this
agent gives it a lifecycle, a place on the dashboard, and a heartbeat the
AgentManager can watch.

It is also the component that notices the dangerous failure: a websocket that
is *connected but silent*. Every price in the system ages while the connection
light stays green, so the agent escalates a quiet feed to a risk event and,
if configured, the kill switch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.events.types import ClockEvent, RiskEvent, StaleDataEvent, Topics
from app.models.enums import ConnectionState

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime


class MarketDataAgent(Agent):
    """Starts the stream, publishes the clock, watches for stale data."""

    agent_type = "market_data"
    inputs = ["alpaca.websocket"]
    outputs = [
        Topics.MARKET_BAR,
        Topics.MARKET_QUOTE,
        Topics.MARKET_TRADE,
        Topics.MARKET_CLOCK,
        Topics.MARKET_STALE,
        Topics.ORDER_UPDATE,
    ]
    tools = ["market_data_service", "broker.get_clock"]
    subscriptions = [Topics.MARKET_STALE]

    def __init__(self, runtime: AtlasRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        # Cycle is just the clock refresh and health report; the data itself
        # arrives by websocket, not by polling.
        self.cycle_interval_seconds = 30.0
        self.staleness_warning_seconds = float(self.config.get("staleness_warning_seconds", 120))
        self._stale_escalated = False

    async def on_start(self) -> None:
        service = self.runtime.data_service
        if service is None:
            self.warn("no market data service available")
            return

        symbols = self.runtime.streaming_symbols()
        self.set_task(f"starting data feed for {len(symbols)} symbols")

        await service.start(
            symbols=symbols,
            subscribe_bars=bool(self.config.get("subscribe_bars", True)),
            subscribe_quotes=bool(self.config.get("subscribe_quotes", True)),
            subscribe_trades=bool(self.config.get("subscribe_trades", False)),
            warmup_timeframe=self.runtime.config.scanner.lookback.timeframe,
            warmup_bars=self.runtime.config.scanner.lookback.bars,
        )

        if not self.runtime.settings.has_alpaca_credentials:
            self.info(
                "running without Alpaca credentials: data comes from the simulator "
                "and no websocket is opened"
            )
        else:
            self.info(f"data feed started for {len(symbols)} symbols")

    async def on_stop(self) -> None:
        if self.runtime.data_service is not None:
            await self.runtime.data_service.stop()

    async def run_cycle(self) -> None:
        service = self.runtime.data_service
        if service is None:
            return

        # Publish the market clock so every agent reads the same session
        # state, from the broker rather than from a local-clock guess.
        try:
            clock = await self.runtime.broker.get_clock()
            self.runtime.market_clock = clock
            await self.publish(ClockEvent(clock=clock, source=self.id))
        except Exception as exc:
            self.warn(f"could not refresh market clock: {exc}")

        status = service.status()
        streams = status.get("streams", {})
        market_stream = streams.get("market", {}) if isinstance(streams, dict) else {}
        state = market_stream.get("state", "disconnected")

        self.set_task(
            f"feed {state} | {status.get('symbol_count', 0)} symbols | "
            f"{market_stream.get('message_count', 0)} messages"
        )

        rejected = market_stream.get("rejected_symbols") or []
        if rejected:
            self.warn(
                f"{len(rejected)} symbols are not being streamed because the data "
                f"plan's symbol limit was reached: {rejected}"
            )

    async def handle_event(self, event: Any) -> None:
        """Escalate a persistently quiet feed."""
        if not isinstance(event, StaleDataEvent):
            return

        # Only escalate while the market is open. A silent feed at 3am is
        # correct behaviour, not a fault.
        clock = self.runtime.market_clock
        if clock is not None and not clock.is_open:
            self.debug("stale data reported but the market is closed; ignoring")
            return

        if self._stale_escalated:
            return
        self._stale_escalated = True

        self.warn(
            f"market data has been stale for "
            f"{event.seconds_since_last_update:.0f}s while the market is open"
        )
        await self.publish(
            RiskEvent(
                severity="critical",
                rule="data_feed_dead",
                detail=(
                    f"No market data for {event.seconds_since_last_update:.0f}s during "
                    f"market hours. Trading on stale prices is unsafe."
                ),
                observed=event.seconds_since_last_update,
                limit=self.staleness_warning_seconds,
                source=self.id,
            )
        )
        await self.runtime.kill_switch.check_auto_trigger(
            "data_feed_dead",
            f"Market data feed silent for {event.seconds_since_last_update:.0f}s "
            f"during market hours",
            broker=self.runtime.broker,
        )

    def detail(self) -> dict[str, Any]:
        service = self.runtime.data_service
        base: dict[str, Any] = {
            "feed": self.runtime.settings.effective_data_feed.value,
            "symbol_limit": self.runtime.settings.effective_max_stream_symbols,
            "has_paid_data_plan": self.runtime.settings.has_paid_data_plan,
            "stale_escalated": self._stale_escalated,
        }
        if service is not None:
            base.update(service.status())
        else:
            base["state"] = ConnectionState.DISCONNECTED.value
        return base
