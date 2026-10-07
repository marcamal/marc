"""Broker adapters.

`app.brokers.base` defines the interfaces; `app.brokers.alpaca` implements them
for Alpaca; `app.brokers.simulated` implements them offline.

Only modules in this package may import a vendor SDK.
"""

from app.brokers.base import (
    BrokerAdapter,
    BrokerAuthError,
    BrokerCapabilities,
    BrokerConnectionError,
    BrokerError,
    DuplicateOrder,
    MarketDataProvider,
    OrderRejected,
    OrderRequest,
)
from app.brokers.factory import build_broker, build_market_data, is_simulated
from app.brokers.simulated import SimulatedBroker, SimulatedMarketData

__all__ = [
    "BrokerAdapter",
    "BrokerAuthError",
    "BrokerCapabilities",
    "BrokerConnectionError",
    "BrokerError",
    "DuplicateOrder",
    "MarketDataProvider",
    "OrderRejected",
    "OrderRequest",
    "SimulatedBroker",
    "SimulatedMarketData",
    "build_broker",
    "build_market_data",
    "is_simulated",
]
