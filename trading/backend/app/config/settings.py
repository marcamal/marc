"""Environment-derived settings and the trading-mode safety gate.

Everything in here comes from the environment or `.env`. Trading *behaviour*
(risk limits, universes, strategy parameters) lives in `config/*.yaml` instead
— see `app.config.schema`.

The most important code in this file is `Settings.effective_mode`, which
decides whether ATLAS is allowed to touch real money. It fails closed: any
ambiguity, any missing flag, any typo resolves to PAPER.
"""

from __future__ import annotations

import enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config/settings.py -> backend/app/config -> backend/app -> backend -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

#: The exact string that must be present in ATLAS_MANUAL_LIVE_CONFIRMATION
#: before live trading is permitted. Deliberately awkward to type by accident.
LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_THE_RISK"


class TradingMode(str, enum.Enum):
    """The four environments ATLAS can run in."""

    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"
    REPLAY = "replay"

    @property
    def uses_real_money(self) -> bool:
        return self is TradingMode.LIVE

    @property
    def touches_broker(self) -> bool:
        """True if this mode talks to a real broker endpoint at all."""
        return self in (TradingMode.PAPER, TradingMode.LIVE)


class StockDataFeed(str, enum.Enum):
    """Alpaca equity data feeds.

    IEX is the only feed available without a paid market-data subscription.
    It is real-time but covers only IEX volume (roughly 2.5% of the market).
    DELAYED_SIP gives full-market data on a 15-minute delay, also free.
    SIP is full market in real time and requires Algo Trader Plus.
    """

    IEX = "iex"
    SIP = "sip"
    DELAYED_SIP = "delayed_sip"


class Settings(BaseSettings):
    """Runtime configuration read from the environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- credentials ---------------------------------------------------------
    # No default beyond "" so that a missing key is an explicit, visible state
    # rather than a crash deep inside the SDK.
    alpaca_api_key: str = Field(default="", alias="ALPACA_API_KEY")
    alpaca_secret_key: str = Field(default="", alias="ALPACA_SECRET_KEY")

    # --- trading mode and live-trading gates --------------------------------
    trading_mode: str = Field(default="paper", alias="ATLAS_TRADING_MODE")
    live_trading_enabled: bool = Field(default=False, alias="ATLAS_LIVE_TRADING_ENABLED")
    manual_live_confirmation: str = Field(default="no", alias="ATLAS_MANUAL_LIVE_CONFIRMATION")

    # --- market data ---------------------------------------------------------
    stock_data_feed: StockDataFeed = Field(default=StockDataFeed.IEX, alias="ATLAS_STOCK_DATA_FEED")
    max_stream_symbols: int = Field(default=30, ge=1, alias="ATLAS_MAX_STREAM_SYMBOLS")
    has_paid_data_plan: bool = Field(default=False, alias="ATLAS_HAS_PAID_DATA_PLAN")

    # --- infrastructure ------------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/atlas.db", alias="ATLAS_DATABASE_URL"
    )
    host: str = Field(default="127.0.0.1", alias="ATLAS_HOST")
    port: int = Field(default=8000, alias="ATLAS_PORT")
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173", alias="ATLAS_CORS_ORIGINS"
    )

    log_level: str = Field(default="INFO", alias="ATLAS_LOG_LEVEL")
    log_format: str = Field(default="console", alias="ATLAS_LOG_FORMAT")

    # --- optional integrations ----------------------------------------------
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    sec_user_agent: str = Field(default="", alias="SEC_USER_AGENT")

    # --- the AI assistant ----------------------------------------------------
    # Entirely optional. With no key ATLAS answers from its own figures; see
    # `app.ai.factory`. The model never gets tools, credentials or a path to
    # an order, so these settings cannot widen what the AI is able to do.
    ai_provider: str = Field(default="auto", alias="ATLAS_AI_PROVIDER")
    ai_model: str = Field(default="", alias="ATLAS_AI_MODEL")
    #: low | medium | high | xhigh | max. Higher means more reasoning, more
    #: tokens and more money per answer.
    ai_effort: str = Field(default="medium", alias="ATLAS_AI_EFFORT")
    ai_max_tokens: int = Field(default=2000, ge=256, le=32000, alias="ATLAS_AI_MAX_TOKENS")
    #: A hard stop on spend. The assistant refuses once the session total
    #: passes this, so a runaway loop cannot quietly cost real money.
    ai_session_budget_usd: float = Field(default=2.00, ge=0.0, alias="ATLAS_AI_SESSION_BUDGET_USD")

    # ------------------------------------------------------------------ #
    # validators
    # ------------------------------------------------------------------ #

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _normalise_mode(cls, v: object) -> str:
        """Lowercase and strip. Unknown values are *kept* so that
        `effective_mode` can log them and fall back to paper, rather than
        silently accepting something that looks like 'live'."""
        return str(v).strip().lower()

    @field_validator("stock_data_feed", mode="before")
    @classmethod
    def _normalise_feed(cls, v: object) -> object:
        if isinstance(v, str):
            cleaned = v.strip().lower()
            # Fail closed to the free feed rather than erroring out.
            return cleaned if cleaned in {f.value for f in StockDataFeed} else "iex"
        return v

    # ------------------------------------------------------------------ #
    # derived properties
    # ------------------------------------------------------------------ #

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requested_mode(self) -> TradingMode:
        """What the environment *asked* for. Not necessarily what it gets."""
        try:
            return TradingMode(self.trading_mode)
        except ValueError:
            return TradingMode.PAPER

    @computed_field  # type: ignore[prop-decorator]
    @property
    def live_gates(self) -> dict[str, bool]:
        """The three independent conditions required for live trading.

        Exposed so the dashboard and the `/api/system/status` endpoint can show
        exactly which gate is closed, instead of a mysterious refusal.
        """
        return {
            "mode_is_live": self.trading_mode == TradingMode.LIVE.value,
            "live_trading_enabled": self.live_trading_enabled,
            "manual_confirmation": self.manual_live_confirmation.strip()
            == LIVE_CONFIRMATION_PHRASE,
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_mode(self) -> TradingMode:
        """The mode ATLAS will actually run in.

        LIVE is granted only when all three gates are open. Everything else
        degrades to PAPER. This is the single chokepoint for that decision —
        no other code may compute it.
        """
        requested = self.requested_mode

        if requested in (TradingMode.BACKTEST, TradingMode.REPLAY):
            return requested

        if requested is TradingMode.LIVE and all(self.live_gates.values()):
            return TradingMode.LIVE

        return TradingMode.PAPER

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_api_key.strip() and self.alpaca_secret_key.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_paper(self) -> bool:
        """True when the Alpaca client should hit the paper endpoint."""
        return self.effective_mode is not TradingMode.LIVE

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def effective_data_feed(self) -> StockDataFeed:
        """Downgrade SIP to IEX unless a paid plan is declared.

        Requesting real-time SIP without the subscription makes Alpaca return
        errors for anything inside the last 15 minutes. Rather than let the
        scanner fail symbol by symbol, we pick a feed that will actually work.
        """
        if self.stock_data_feed is StockDataFeed.SIP and not self.has_paid_data_plan:
            return StockDataFeed.IEX
        return self.stock_data_feed

    @property
    def has_ai_credentials(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @property
    def effective_ai_effort(self) -> str:
        """A validated effort level.

        An unknown value falls back to `medium` rather than erroring: a typo
        in an optional setting should not make the assistant unusable.
        """
        level = self.ai_effort.strip().lower()
        return level if level in ("low", "medium", "high", "xhigh", "max") else "medium"

    @property
    def effective_max_stream_symbols(self) -> int:
        """The free plan caps one websocket at 30 symbols."""
        if self.has_paid_data_plan:
            return self.max_stream_symbols
        return min(self.max_stream_symbols, 30)

    def mode_warnings(self) -> list[str]:
        """Human-readable notes about the current configuration.

        Surfaced in the logs at startup and on the System page, so that a
        misconfiguration is obvious instead of mysterious.
        """
        notes: list[str] = []

        if self.trading_mode not in {m.value for m in TradingMode}:
            notes.append(
                f"ATLAS_TRADING_MODE='{self.trading_mode}' is not recognised. "
                f"Falling back to PAPER."
            )

        if self.requested_mode is TradingMode.LIVE and self.effective_mode is not TradingMode.LIVE:
            closed = [name for name, open_ in self.live_gates.items() if not open_]
            notes.append(
                "LIVE trading was requested but is BLOCKED. Closed gates: " + ", ".join(closed)
            )

        if not self.has_alpaca_credentials:
            notes.append(
                "No Alpaca credentials found. ATLAS will run with the simulated "
                "broker so you can explore the system offline."
            )

        if self.stock_data_feed is StockDataFeed.SIP and not self.has_paid_data_plan:
            notes.append(
                "SIP feed requested without ATLAS_HAS_PAID_DATA_PLAN=true. "
                "Downgraded to IEX, which is the free real-time feed."
            )

        if not self.has_paid_data_plan and self.max_stream_symbols > 30:
            notes.append(
                "The free Alpaca data plan allows 30 streaming symbols. "
                f"ATLAS_MAX_STREAM_SYMBOLS={self.max_stream_symbols} capped to 30."
            )

        return notes


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that every module sees the same object. Tests clear the cache
    with `get_settings.cache_clear()` after patching the environment.
    """
    return Settings()  # type: ignore[call-arg]
