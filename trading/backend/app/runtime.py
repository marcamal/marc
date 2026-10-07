"""AtlasRuntime — the composition root.

Everything with a lifetime longer than a request lives here: settings, config,
the event bus, the broker, the market data service, the kill switch, the
agents, the strategies, the database. One object, constructed once at startup
and shut down once at exit.

Why a container rather than module-level globals: tests construct a runtime
with a simulated broker and an in-memory database in three lines, with no
monkeypatching and no import-order problems. It also makes the dependency
graph explicit — if something is not on this object, no agent can reach it.

Startup order is deliberate and documented inline; several steps depend on
earlier ones in ways that are not obvious.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.brokers.base import BrokerAdapter, BrokerCapabilities, BrokerError, MarketDataProvider
from app.brokers.factory import build_broker, build_market_data
from app.config.loader import get_config
from app.config.schema import AtlasConfig
from app.config.settings import Settings, TradingMode, get_settings
from app.core.logging import get_logger
from app.database.repository import Queries, Recorder
from app.database.session import Database
from app.events.bus import EventBus
from app.events.types import SystemEvent, Topics
from app.execution.tracker import OrderTracker
from app.market_data.service import MarketDataService
from app.models.market import MarketClock
from app.models.trading import PortfolioSnapshot
from app.risk.kill_switch import KillSwitch
from app.strategies.base import StrategyRegistry

if TYPE_CHECKING:
    from app.agents.manager import AgentManager

log = get_logger(__name__)


class AtlasRuntime:
    """The application's shared state and services."""

    def __init__(
        self,
        settings: Settings | None = None,
        config: AtlasConfig | None = None,
        broker: BrokerAdapter | None = None,
        market_data: MarketDataProvider | None = None,
        database: Database | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.config = config or get_config()

        self.bus = EventBus()
        # Injectable so tests can pass a SimulatedBroker without touching
        # the environment.
        self.broker: BrokerAdapter = broker or build_broker(self.settings)
        self.market_data: MarketDataProvider = market_data or build_market_data(self.settings)

        self.database = database or Database(self.settings.database_url)
        self.queries = Queries(self.database)
        self.recorder = Recorder(
            self.database, self.bus, trading_mode=self.settings.effective_mode.value
        )

        self.kill_switch = KillSwitch(self.config.risk.kill_switch, bus=self.bus)
        self.order_tracker = OrderTracker()
        self.strategies = StrategyRegistry(self.config.strategies)

        self.data_service: MarketDataService | None = None
        self.agents: AgentManager | None = None  # type: ignore[assignment]

        # --- live shared state, updated by agents --------------------------
        self.portfolio_snapshot: PortfolioSnapshot | None = None
        self.market_clock: MarketClock | None = None
        self.capabilities: BrokerCapabilities | None = None

        self.started_at: datetime | None = None
        self.startup_errors: list[str] = []
        self._started = False

    # ------------------------------------------------------------------ #
    # startup / shutdown
    # ------------------------------------------------------------------ #

    async def startup(self) -> None:
        """Bring ATLAS up.

        Ordering requirements, which is why this is a sequence and not a
        `gather`:

        1. Log the mode first, so the very first line of output says whether
           real money is at risk.
        2. Database before the recorder, which needs its tables.
        3. Bus before agents, which subscribe on start.
        4. Capabilities before agents, because the Risk Agent's very first
           decision needs to know what the account supports.
        5. Agents last. The Risk Agent must be live before the Execution
           Agent will accept anything.
        """
        if self._started:
            return

        self.started_at = datetime.now(UTC)
        self._log_mode_banner()

        # --- 2. database -----------------------------------------------
        try:
            await self.database.create_schema()
        except Exception as exc:
            # A missing journal is not a reason to refuse to trade on paper,
            # but it must be visible on the System page.
            message = f"database unavailable: {exc}"
            self.startup_errors.append(message)
            log.error(message)

        # --- 3. bus + recorder ------------------------------------------
        await self.bus.start()
        self.recorder.start()

        # --- 4. broker capabilities -------------------------------------
        await self.get_capabilities()

        # --- market data service ----------------------------------------
        market_data_entry = self.config.agents.by_id("market_data")
        staleness_seconds = (
            float(market_data_entry.config.get("staleness_warning_seconds", 120))
            if market_data_entry
            else 120.0
        )
        self.data_service = MarketDataService(
            provider=self.market_data,
            bus=self.bus,
            settings=self.settings,
            staleness_warning_seconds=staleness_seconds,
        )

        # --- strategies --------------------------------------------------
        self.strategies.load_all()
        active = self.strategies.active
        if active:
            log.warning(
                "ACTIVE STRATEGIES: %s — these can generate trade proposals",
                [s.id for s in active],
            )
        else:
            log.info(
                "no active strategies: nothing will generate trade proposals until "
                "you enable one in strategies.yaml or from the dashboard"
            )

        # --- 5. agents ---------------------------------------------------
        from app.agents import build_agents
        from app.agents.manager import AgentManager

        self.agents = AgentManager(
            bus=self.bus,
            health_check_interval_seconds=self.config.agents.health_check_interval_seconds,
            heartbeat_timeout_seconds=self.config.agents.heartbeat_timeout_seconds,
        )
        build_agents(self, self.agents)
        await self.agents.start_all()

        if self.kill_switch.is_engaged:
            log.critical(
                "KILL SWITCH IS ENGAGED at startup (%s). No orders will be placed. "
                "Delete %s to resume.",
                self.kill_switch.reason or "file present",
                self.kill_switch.file_path,
            )

        await self.bus.publish(
            SystemEvent(
                topic=Topics.SYSTEM_STARTED,
                detail=f"ATLAS started in {self.settings.effective_mode.value.upper()} mode",
                context={
                    "mode": self.settings.effective_mode.value,
                    "broker": self.broker.name,
                    "agents": len(self.agents.agents),
                },
                source="runtime",
            )
        )

        self._started = True
        log.info("ATLAS startup complete")

    async def shutdown(self) -> None:
        """Bring ATLAS down cleanly, in reverse order."""
        if not self._started:
            return

        await self.bus.publish(
            SystemEvent(topic=Topics.SYSTEM_STOPPING, detail="shutting down", source="runtime")
        )

        if self.agents is not None:
            await self.agents.stop_all()
        if self.data_service is not None:
            await self.data_service.stop()

        await self.recorder.stop()
        await self.bus.stop()
        await self.database.dispose()

        self._started = False
        log.info("ATLAS shutdown complete")

    @property
    def is_started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _log_mode_banner(self) -> None:
        """Make the trading mode impossible to miss in the logs."""
        mode = self.settings.effective_mode

        if mode is TradingMode.LIVE:
            log.critical("=" * 72)
            log.critical("  ATLAS IS RUNNING IN **LIVE** MODE — REAL MONEY IS AT RISK")
            log.critical("=" * 72)
        else:
            log.info("=" * 72)
            log.info("  ATLAS — mode: %s (no real money)", mode.value.upper())
            log.info("=" * 72)

        for warning in self.settings.mode_warnings():
            log.warning(warning)

    async def get_capabilities(self) -> BrokerCapabilities:
        """Detected broker capabilities, cached.

        Falls back to conservative defaults when detection fails, so a broker
        outage at startup cannot cause ATLAS to assume it may short on margin.
        """
        if self.capabilities is not None:
            return self.capabilities
        try:
            capabilities = await self.broker.get_capabilities()
        except BrokerError as exc:
            message = f"could not detect broker capabilities: {exc}"
            self.startup_errors.append(message)
            log.error(message)
            capabilities = BrokerCapabilities(
                broker_name=self.broker.name,
                detected=False,
                notes=[
                    message,
                    "Using conservative defaults: no crypto, options, "
                    "shorting, margin or fractional shares.",
                ],
            )

        # Data-plan facts come from our own settings, not the trading API.
        capabilities.data_feed = self.settings.effective_data_feed.value
        capabilities.max_stream_symbols = self.settings.effective_max_stream_symbols
        capabilities.has_paid_data_plan = self.settings.has_paid_data_plan

        self.capabilities = capabilities
        return capabilities

    def streaming_symbols(self) -> list[str]:
        """Symbols the market data service should stream.

        The scanner universe plus every strategy's symbols, deduplicated and
        truncated to the plan's websocket budget. Truncation happens inside
        the stream (which reports what it dropped), but ordering here means
        the symbols that matter most survive it: strategy symbols first,
        because those are the ones that can actually produce a trade.
        """
        symbols: list[str] = []

        for strategy in self.strategies.all:
            for symbol in strategy.symbols:
                if symbol not in symbols:
                    symbols.append(symbol)

        for symbol in self.config.scanner.active_symbols:
            if symbol not in symbols:
                symbols.append(symbol)

        return symbols

    async def engage_kill_switch(self, reason: str, triggered_by: str = "manual") -> Any:
        """Engage the kill switch, with the broker attached for cancellation."""
        return await self.kill_switch.engage(
            reason=reason, triggered_by=triggered_by, broker=self.broker
        )

    async def release_kill_switch(self, released_by: str = "manual") -> Any:
        return await self.kill_switch.release(released_by=released_by)

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        """The payload behind `/api/system/status`."""
        settings = self.settings
        uptime = (datetime.now(UTC) - self.started_at).total_seconds() if self.started_at else 0.0

        return {
            "version": "0.1.0",
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "uptime_seconds": round(uptime, 1),
            "mode": {
                "effective": settings.effective_mode.value,
                "requested": settings.requested_mode.value,
                "is_live": settings.effective_mode is TradingMode.LIVE,
                "live_gates": settings.live_gates,
                "warnings": settings.mode_warnings(),
            },
            "broker": {
                "name": self.broker.name,
                "simulated": self.broker.name == "simulated",
                "has_credentials": settings.has_alpaca_credentials,
                "endpoint": "paper" if settings.is_paper else "live",
                "capabilities": (self.capabilities.model_dump() if self.capabilities else None),
            },
            "market_data": (
                self.data_service.status()
                if self.data_service
                else {"started": False, "feed": settings.effective_data_feed.value}
            ),
            "market_clock": (
                self.market_clock.model_dump(mode="json") if self.market_clock else None
            ),
            "kill_switch": self.kill_switch.status(),
            "agents": (self.agents.health_summary() if self.agents else {}),
            "strategies": self.strategies.status(),
            "orders": self.order_tracker.status(),
            "database": {
                "url": self.database._safe_url(),
                "size_bytes": self.database.file_size_bytes(),
                "recorder": self.recorder.status(),
            },
            "event_bus": self.bus.stats(),
            "startup_errors": self.startup_errors,
        }
