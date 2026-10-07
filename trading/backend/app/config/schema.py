"""Typed models for the YAML files in `config/`.

Validating configuration into Pydantic models at startup means a typo in
`risk.yaml` is a clear error on line 1 of the logs, not a `KeyError` three
hours into a trading session.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    """Reject unknown keys.

    A silently-ignored `max_risk_per_trade_pc` (note the typo) would mean
    trading with the default limit while believing it was configured. For risk
    settings that is unacceptable, so unknown keys are a hard error.
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# risk.yaml
# --------------------------------------------------------------------------- #


class KillSwitchConfig(_Strict):
    file_path: str = "data/KILL_SWITCH"
    block_new_orders: bool = True
    cancel_open_orders: bool = True
    flatten_positions: bool = False
    auto_triggers: dict[str, bool] = Field(default_factory=dict)
    repeated_rejection_threshold: int = Field(default=5, ge=1)


class RiskConfig(_Strict):
    # per trade
    max_risk_per_trade_pct: float = Field(default=0.5, gt=0, le=100)
    max_position_pct: float = Field(default=5.0, gt=0, le=100)
    max_order_notional: float = Field(default=2000.0, gt=0)
    min_order_notional: float = Field(default=10.0, ge=0)

    # daily
    max_daily_loss_pct: float = Field(default=2.0, gt=0, le=100)
    max_orders_per_day: int = Field(default=20, ge=0)
    max_orders_per_symbol_per_day: int = Field(default=3, ge=0)

    # portfolio
    max_portfolio_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
    max_total_exposure_pct: float = Field(default=60.0, gt=0, le=1000)
    max_sector_exposure_pct: float = Field(default=25.0, gt=0, le=100)
    max_correlated_exposure_pct: float = Field(default=30.0, gt=0, le=100)
    max_open_positions: int = Field(default=8, ge=0)
    max_leverage: float = Field(default=1.0, ge=0, le=4)

    # market quality
    min_average_volume: int = Field(default=500_000, ge=0)
    max_spread_pct: float = Field(default=0.5, gt=0)
    max_data_staleness_seconds: int = Field(default=60, ge=0)
    require_market_open: bool = True

    correlation_groups: dict[str, list[str]] = Field(default_factory=dict)
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)

    required_proposal_fields: list[str] = Field(
        default_factory=lambda: [
            "symbol",
            "side",
            "strategy_id",
            "entry_price",
            "stop_price",
            "quantity",
            "created_at",
        ]
    )
    min_reward_risk_ratio: float = Field(default=1.5, ge=0)
    max_proposal_age_seconds: int = Field(default=300, ge=1)

    def correlation_group_for(self, symbol: str) -> str | None:
        """Which correlation bucket a symbol belongs to, if any.

        A symbol may appear in several groups (VRT is both energy_power and
        hvac_cooling); the first match wins, which keeps the exposure check
        deterministic.
        """
        for group, symbols in self.correlation_groups.items():
            if symbol in symbols:
                return group
        return None


# --------------------------------------------------------------------------- #
# scanner.yaml
# --------------------------------------------------------------------------- #


class ScannerLookback(_Strict):
    timeframe: str = "5Min"
    bars: int = Field(default=120, ge=2)
    daily_bars: int = Field(default=60, ge=2)


class ScannerIndicators(_Strict):
    ema_fast: int = Field(default=20, ge=2)
    ema_slow: int = Field(default=50, ge=2)
    sma_long: int = Field(default=200, ge=2)
    rsi_period: int = Field(default=14, ge=2)
    atr_period: int = Field(default=14, ge=2)
    bollinger_period: int = Field(default=20, ge=2)
    bollinger_std: float = Field(default=2.0, gt=0)
    momentum_period: int = Field(default=10, ge=1)


class ScannerFilters(_Strict):
    min_price: float = Field(default=5.0, ge=0)
    max_price: float = Field(default=2000.0, gt=0)
    min_volume: int = Field(default=300_000, ge=0)
    max_spread_percent: float = Field(default=1.0, gt=0)
    exclude_halted: bool = True


class ScannerRanking(_Strict):
    weights: dict[str, float] = Field(default_factory=dict)
    max_results: int = Field(default=20, ge=1)
    min_score: float = 0.0


class ScannerConfig(_Strict):
    scan_interval_seconds: int = Field(default=60, ge=5)
    lookback: ScannerLookback = Field(default_factory=ScannerLookback)
    active: str = "core_watchlist"
    universes: dict[str, list[str]] = Field(default_factory=dict)
    metrics: dict[str, bool] = Field(default_factory=dict)
    indicators: ScannerIndicators = Field(default_factory=ScannerIndicators)
    filters: ScannerFilters = Field(default_factory=ScannerFilters)
    ranking: ScannerRanking = Field(default_factory=ScannerRanking)

    @model_validator(mode="after")
    def _active_universe_exists(self) -> ScannerConfig:
        if self.universes and self.active not in self.universes:
            raise ValueError(
                f"scanner.yaml: active universe '{self.active}' is not defined. "
                f"Available: {sorted(self.universes)}"
            )
        return self

    @property
    def active_symbols(self) -> list[str]:
        return list(self.universes.get(self.active, []))


# --------------------------------------------------------------------------- #
# strategies.yaml
# --------------------------------------------------------------------------- #


class StrategyRiskConfig(_Strict):
    max_risk_per_trade_pct: float = Field(default=0.25, gt=0, le=100)
    max_concurrent_positions: int = Field(default=2, ge=0)
    max_orders_per_day: int = Field(default=4, ge=0)
    session_start: str = "10:00"
    session_end: str = "15:30"
    close_before_market_close_minutes: int = Field(default=20, ge=0)


class StrategyConfig(_Strict):
    id: str
    name: str
    module: str
    cls: str = Field(alias="class")
    enabled: bool = False
    description: str = ""
    symbols: list[str] = Field(default_factory=list)
    timeframe: str = "5Min"
    params: dict[str, float | int | str | bool] = Field(default_factory=dict)
    risk: StrategyRiskConfig = Field(default_factory=StrategyRiskConfig)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StrategiesConfig(_Strict):
    strategies_globally_enabled: bool = False
    strategies: list[StrategyConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> StrategiesConfig:
        ids = [s.id for s in self.strategies]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"strategies.yaml: duplicate strategy ids {sorted(duplicates)}")
        return self

    def by_id(self, strategy_id: str) -> StrategyConfig | None:
        return next((s for s in self.strategies if s.id == strategy_id), None)


# --------------------------------------------------------------------------- #
# agents.yaml
# --------------------------------------------------------------------------- #


class AgentConfigEntry(_Strict):
    id: str
    name: str
    role: str
    type: str
    autostart: bool = False
    config: dict[str, object] = Field(default_factory=dict)


class AgentsConfig(_Strict):
    health_check_interval_seconds: int = Field(default=15, ge=1)
    heartbeat_timeout_seconds: int = Field(default=90, ge=1)
    log_buffer_size: int = Field(default=200, ge=10)
    agents: list[AgentConfigEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> AgentsConfig:
        ids = [a.id for a in self.agents]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"agents.yaml: duplicate agent ids {sorted(duplicates)}")
        return self

    def by_id(self, agent_id: str) -> AgentConfigEntry | None:
        return next((a for a in self.agents if a.id == agent_id), None)


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #


class AtlasConfig(BaseModel):
    """Every YAML config, validated, in one object."""

    risk: RiskConfig
    scanner: ScannerConfig
    strategies: StrategiesConfig
    agents: AgentsConfig
