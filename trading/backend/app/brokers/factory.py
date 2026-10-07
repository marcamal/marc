"""Chooses the broker implementation for the current mode.

The rule, in one sentence: **you get the live endpoint only if you asked for
it through all three gates AND supplied credentials.** Everything else gets
paper or simulation.
"""

from __future__ import annotations

from app.brokers.base import BrokerAdapter, MarketDataProvider
from app.brokers.simulated import SimulatedBroker, SimulatedMarketData
from app.config.settings import Settings, TradingMode
from app.core.logging import get_logger

log = get_logger(__name__)


def build_broker(settings: Settings) -> BrokerAdapter:
    """Return the broker for this configuration.

    Falls back to `SimulatedBroker` when credentials are absent, so that ATLAS
    starts and the dashboard works before the operator has an Alpaca account.
    """
    mode = settings.effective_mode

    if mode in (TradingMode.BACKTEST, TradingMode.REPLAY):
        log.info("broker: simulated (mode=%s)", mode.value)
        return SimulatedBroker()

    if not settings.has_alpaca_credentials:
        log.warning(
            "broker: simulated — no Alpaca credentials found. "
            "Add ALPACA_API_KEY and ALPACA_SECRET_KEY to .env to connect for real."
        )
        return SimulatedBroker()

    # Imported here so that the simulated path never needs the SDK installed.
    from app.brokers.alpaca.broker import AlpacaBroker

    is_paper = settings.is_paper
    if not is_paper:
        # The only place in ATLAS that can reach a live account. Shout about it.
        log.critical("=" * 70 + "\n  BROKER: ALPACA **LIVE** — REAL MONEY IS AT RISK\n" + "=" * 70)
    else:
        log.info("broker: Alpaca PAPER endpoint")

    return AlpacaBroker(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=is_paper,
    )


def build_market_data(settings: Settings) -> MarketDataProvider:
    """Return the market data provider for this configuration."""
    if not settings.has_alpaca_credentials:
        log.warning("market data: simulated — no Alpaca credentials found.")
        return SimulatedMarketData()

    if settings.effective_mode in (TradingMode.BACKTEST, TradingMode.REPLAY):
        # Phase 2 replaces this with a historical replay provider that serves
        # recorded bars through the same interface.
        return SimulatedMarketData()

    from app.brokers.alpaca.market_data import AlpacaMarketData

    feed = settings.effective_data_feed.value
    log.info("market data: Alpaca", extra={"feed": feed})
    return AlpacaMarketData(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        feed=feed,
    )


def is_simulated(broker: BrokerAdapter) -> bool:
    return broker.name == "simulated"
