"""Configuration: environment settings plus validated YAML config."""

from app.config.loader import ConfigError, get_config, load_config
from app.config.settings import (
    CONFIG_DIR,
    DATA_DIR,
    LIVE_CONFIRMATION_PHRASE,
    LOGS_DIR,
    PROJECT_ROOT,
    Settings,
    StockDataFeed,
    TradingMode,
    get_settings,
)

__all__ = [
    "CONFIG_DIR",
    "DATA_DIR",
    "LIVE_CONFIRMATION_PHRASE",
    "LOGS_DIR",
    "PROJECT_ROOT",
    "ConfigError",
    "Settings",
    "StockDataFeed",
    "TradingMode",
    "get_config",
    "get_settings",
    "load_config",
]
