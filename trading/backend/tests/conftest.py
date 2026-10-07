"""Shared test fixtures.

Two guarantees this file is responsible for:

1.  **No test ever reaches the network.** Every fixture wires in
    `SimulatedBroker` / `SimulatedMarketData`. There is also an autouse
    fixture that clears any real Alpaca credentials out of the environment,
    so running the suite on a machine with a populated `.env` cannot
    accidentally talk to a broker.

2.  **No test ever places a real order.** The simulated broker keeps
    everything in memory.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.brokers.base import BrokerCapabilities
from app.brokers.simulated import SimulatedBroker, SimulatedMarketData
from app.config.loader import load_config
from app.config.schema import AtlasConfig
from app.config.settings import PROJECT_ROOT, Settings, TradingMode
from app.core.logging import configure_logging
from app.database.session import Database
from app.events.bus import EventBus
from app.models.enums import OrderType, Side, TimeInForce
from app.models.market import Bar, MarketClock, Quote
from app.models.trading import AccountSnapshot, PortfolioSnapshot, TradeProposal
from app.risk.rules import RiskContext
from app.runtime import AtlasRuntime

#: The real config directory. Tests validate against the shipped YAML, so a
#: change that breaks a risk limit is caught by the suite.
CONFIG_DIR = PROJECT_ROOT / "config"


@pytest.fixture(scope="session", autouse=True)
def _configure_test_logging() -> None:
    configure_logging(level="WARNING", fmt="console")


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip broker credentials from the environment for every test.

    Without this, running the suite on the operator's own machine would pick
    up their real keys from the environment. Tests must be hermetic.
    """
    for name in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ATLAS_TRADING_MODE",
        "ATLAS_LIVE_TRADING_ENABLED",
        "ATLAS_MANUAL_LIVE_CONFIRMATION",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    """Default settings with no `.env` file and no credentials."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture(scope="session")
def _config_template() -> AtlasConfig:
    """The shipped YAML, loaded once. Never handed to a test directly."""
    return load_config(CONFIG_DIR)


@pytest.fixture
def config(_config_template: AtlasConfig) -> AtlasConfig:
    """A fresh deep copy of the real configuration for each test.

    Copied rather than shared: several tests mutate a limit to prove a rule
    fires (or that a hard ceiling clamps it), and a shared instance would leak
    that mutation into every later test in the session.
    """
    return _config_template.model_copy(deep=True)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
async def started_bus(bus: EventBus) -> AsyncIterator[EventBus]:
    await bus.start()
    try:
        yield bus
    finally:
        await bus.stop()


@pytest.fixture
def market_data() -> SimulatedMarketData:
    return SimulatedMarketData(seed=42)


@pytest.fixture
def broker(market_data: SimulatedMarketData) -> SimulatedBroker:
    return SimulatedBroker(starting_equity=100_000.0, market_data=market_data)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    await db.create_schema()
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
def kill_switch_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the kill switch file into a temp dir.

    Essential: without it a test that engages the switch would write
    `data/KILL_SWITCH` into the real project and block the operator's own
    trading until they noticed.
    """
    monkeypatch.setattr("app.risk.kill_switch.PROJECT_ROOT", tmp_path)
    return tmp_path / "data" / "KILL_SWITCH"


@pytest.fixture
async def runtime(
    settings: Settings,
    config: AtlasConfig,
    broker: SimulatedBroker,
    market_data: SimulatedMarketData,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AtlasRuntime]:
    """A fully started runtime on the simulated broker."""
    monkeypatch.setattr("app.risk.kill_switch.PROJECT_ROOT", tmp_path)

    rt = AtlasRuntime(
        settings=settings,
        config=config,
        broker=broker,
        market_data=market_data,
        database=database,
    )
    await rt.startup()
    try:
        yield rt
    finally:
        await rt.shutdown()


# --------------------------------------------------------------------------- #
# model builders
# --------------------------------------------------------------------------- #


@pytest.fixture
def account() -> AccountSnapshot:
    return AccountSnapshot(
        account_id="TEST",
        equity=100_000.0,
        last_equity=100_000.0,
        cash=100_000.0,
        buying_power=100_000.0,
        multiplier=1.0,
    )


@pytest.fixture
def capabilities() -> BrokerCapabilities:
    """A plain cash-account profile: no margin, shorting, crypto or options."""
    return BrokerCapabilities(
        broker_name="test",
        detected=True,
        supports_crypto=False,
        supports_options=False,
        supports_short_selling=False,
        supports_fractional_shares=False,
        margin_enabled=False,
        max_leverage=1.0,
    )


@pytest.fixture
def fresh_quote() -> Quote:
    return Quote(
        symbol="SPY",
        timestamp=datetime.now(UTC),
        bid_price=584.95,
        bid_size=500,
        ask_price=585.05,
        ask_size=500,
    )


@pytest.fixture
def open_clock() -> MarketClock:
    return MarketClock(timestamp=datetime.now(UTC), is_open=True)


@pytest.fixture
def risk_context(
    account: AccountSnapshot,
    config: AtlasConfig,
    capabilities: BrokerCapabilities,
    fresh_quote: Quote,
    open_clock: MarketClock,
) -> RiskContext:
    """A context in which a sane proposal is expected to pass every rule."""
    return RiskContext(
        account=account,
        portfolio=PortfolioSnapshot(
            equity=account.equity,
            cash=account.cash,
            buying_power=account.buying_power,
            high_water_mark=account.equity,
        ),
        config=config.risk,
        mode=TradingMode.PAPER,
        capabilities=capabilities,
        clock=open_clock,
        quote=fresh_quote,
        average_volume=50_000_000.0,
        now=datetime.now(UTC),
    )


@pytest.fixture
def valid_proposal() -> TradeProposal:
    """A coherent, modestly sized long proposal."""
    return TradeProposal(
        strategy_id="test_strategy",
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        entry_price=585.0,
        stop_price=580.0,
        target_price=600.0,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        confidence=0.7,
    )


def make_bars(
    symbol: str = "TEST",
    count: int = 60,
    start_price: float = 100.0,
    trend: float = 0.0,
    volume: float = 1_000_000.0,
    high_offset: float = 0.5,
    low_offset: float = 0.5,
    wobble: float = 0.0,
    interval_minutes: int = 5,
) -> list[Bar]:
    """Deterministic bars with a controllable linear trend.

    `wobble` superimposes a sawtooth on the trend. It matters because a
    perfectly linear rise produces RSI exactly 100 — mathematically correct
    (there are no down moves at all) but unlike any real market, and it fails
    any strategy with an overbought filter. A small wobble gives a rising
    series with realistic pullbacks.
    """
    from datetime import timedelta

    anchor = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    bars: list[Bar] = []
    for index in range(count):
        # Every third bar pulls back, so RSI lands in a realistic range.
        offset = -wobble if index % 3 == 2 else wobble / 2
        close = start_price + trend * index + offset
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=anchor + timedelta(minutes=interval_minutes * index),
                open=close - trend / 2,
                high=close + high_offset,
                low=close - low_offset,
                close=close,
                volume=volume,
                vwap=close,
            )
        )
    return bars


@pytest.fixture
def bars_uptrend() -> list[Bar]:
    return make_bars(count=120, start_price=100.0, trend=0.25)


@pytest.fixture
def bars_flat() -> list[Bar]:
    return make_bars(count=120, start_price=100.0, trend=0.0)


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear every ATLAS_* variable, for settings tests."""
    for key in list(os.environ):
        if key.startswith("ATLAS_") or key.startswith("ALPACA_"):
            monkeypatch.delenv(key, raising=False)
    yield
