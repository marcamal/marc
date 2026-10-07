"""Strategy interface and registry.

A strategy is a pure-ish function from market data to zero or more
`TradeProposal` objects. The constraints are deliberate:

*   **A strategy may never call the broker.** It cannot place, modify or
    cancel an order. It returns proposals; the pipeline does the rest.
*   **A strategy may never bypass risk.** There is no path from here to
    execution that skips the Risk Agent.
*   **A strategy is disabled until explicitly enabled.** Both a global switch
    (`strategies_globally_enabled`) and a per-strategy flag must be on.

The same `generate()` method is what the backtesting engine will call, which
is how ATLAS avoids the classic trap of a "backtest version" and a "live
version" of a strategy drifting apart until the backtest is meaningless.
"""

from __future__ import annotations

import abc
import importlib
from datetime import UTC, datetime
from typing import Any

from app.config.schema import StrategiesConfig, StrategyConfig
from app.core.logging import get_logger
from app.models.market import Bar, IndicatorSet
from app.models.trading import AccountSnapshot, Position, TradeProposal

log = get_logger(__name__)


class StrategyContext:
    """Everything a strategy may look at.

    Passing a context object rather than letting strategies reach into global
    services is what keeps them testable and backtestable: the backtest engine
    constructs the same context from historical data.
    """

    def __init__(
        self,
        account: AccountSnapshot,
        positions: list[Position],
        bars: dict[str, list[Bar]],
        indicators: dict[str, IndicatorSet],
        now: datetime | None = None,
        market_open: bool = True,
        seconds_until_close: float | None = None,
    ) -> None:
        self.account = account
        self.positions = positions
        self.bars = bars
        self.indicators = indicators
        self.now = now or datetime.now(UTC)
        self.market_open = market_open
        self.seconds_until_close = seconds_until_close

    def position_for(self, symbol: str) -> Position | None:
        return next((p for p in self.positions if p.symbol == symbol), None)

    def holds(self, symbol: str) -> bool:
        position = self.position_for(symbol)
        return bool(position and abs(position.quantity) > 1e-9)


class Strategy(abc.ABC):
    """Base class for all strategies."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.id = config.id
        self.name = config.name
        self.params: dict[str, Any] = dict(config.params)
        self.symbols = list(config.symbols)
        self.timeframe = config.timeframe
        self._enabled = config.enabled
        self.log = get_logger(f"strategy.{self.id}")

        # Counters for the Strategies page.
        self.proposals_generated = 0
        self.last_run_at: datetime | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ #
    # enable / disable
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        """Turn the strategy on for this session only.

        Not persisted to `strategies.yaml` on purpose: a restart returns to the
        configured state, so an experiment cannot quietly become permanent.
        """
        self._enabled = True
        self.log.warning("strategy ENABLED (this session only)", extra={"strategy": self.id})

    def disable(self) -> None:
        self._enabled = False
        self.log.info("strategy disabled", extra={"strategy": self.id})

    # ------------------------------------------------------------------ #
    # the strategy contract
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def generate(self, context: StrategyContext) -> list[TradeProposal]:
        """Produce trade proposals from the current context.

        Must be side-effect free: no IO, no order placement, no mutation of
        the context. Return an empty list when there is nothing to do, which
        is the common case and the correct answer most of the time.
        """

    def required_bar_count(self) -> int:
        """How much history this strategy needs, for cache warmup."""
        periods = [
            int(v)
            for v in self.params.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        return max(100, (max(periods) if periods else 50) * 3)

    def stats(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self._enabled,
            "symbols": self.symbols,
            "timeframe": self.timeframe,
            "params": self.params,
            "proposals_generated": self.proposals_generated,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "description": self.config.description,
            "risk": self.config.risk.model_dump(),
        }


class StrategyRegistry:
    """Loads strategies from config and holds them.

    Strategies are imported by dotted path from `strategies.yaml`, so adding
    one is a config change plus a new module — no edits to the framework.
    """

    def __init__(self, config: StrategiesConfig) -> None:
        self._config = config
        self._strategies: dict[str, Strategy] = {}

    @property
    def globally_enabled(self) -> bool:
        return self._config.strategies_globally_enabled

    def load_all(self) -> list[Strategy]:
        """Import and instantiate every strategy listed in the config.

        A strategy that fails to import is logged and skipped rather than
        taking the whole application down: one broken experimental strategy
        should not stop ATLAS from starting.
        """
        for entry in self._config.strategies:
            try:
                module = importlib.import_module(entry.module)
                cls = getattr(module, entry.cls)
                strategy = cls(entry)
                if not isinstance(strategy, Strategy):
                    raise TypeError(f"{entry.module}.{entry.cls} is not a Strategy subclass")
                self._strategies[entry.id] = strategy
                log.info(
                    "strategy loaded",
                    extra={
                        "strategy": entry.id,
                        "enabled": entry.enabled,
                        "symbols": len(entry.symbols),
                    },
                )
            except Exception as exc:
                log.error(
                    "failed to load strategy '%s' from %s.%s: %s",
                    entry.id,
                    entry.module,
                    entry.cls,
                    exc,
                )
        return list(self._strategies.values())

    def get(self, strategy_id: str) -> Strategy | None:
        return self._strategies.get(strategy_id)

    @property
    def all(self) -> list[Strategy]:
        return list(self._strategies.values())

    @property
    def active(self) -> list[Strategy]:
        """Strategies that may actually run right now.

        Both gates must be open. The global switch exists so that everything
        can be stopped without editing each strategy.
        """
        if not self.globally_enabled:
            return []
        return [s for s in self._strategies.values() if s.enabled]

    def config_for(self, strategy_id: str) -> StrategyConfig | None:
        return self._config.by_id(strategy_id)

    def status(self) -> dict[str, Any]:
        return {
            "globally_enabled": self.globally_enabled,
            "loaded": len(self._strategies),
            "active": len(self.active),
            "strategies": [s.stats() for s in self._strategies.values()],
        }
