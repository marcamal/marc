"""Market Scanner Agent — ranks the configured universe.

Produces `ScannerResult` objects and nothing else. **The scanner never
trades.** Its output is an observation that research and strategy agents may
choose to look at.

Everything is computed from the batched snapshot and bar requests, so one scan
cycle over a 30-symbol universe costs two REST calls rather than sixty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.brokers.base import BrokerError
from app.events.types import ScannerResultsEvent, Topics
from app.market_data.indicators import compute_indicators
from app.market_data.scanner import apply_filters, describe, rank_candidates
from app.models.market import ScannerResult

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime


class ScannerAgent(Agent):
    """Periodically scores and ranks a symbol universe."""

    agent_type = "scanner"
    inputs = [Topics.MARKET_BAR, Topics.MARKET_QUOTE]
    outputs = [Topics.SCANNER_RESULTS]
    tools = ["market_data.get_snapshots", "market_data.get_bars", "indicators"]
    subscriptions: list[str] = []

    def __init__(self, runtime: AtlasRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        scanner_config = runtime.config.scanner
        self.cycle_interval_seconds = float(scanner_config.scan_interval_seconds)
        self.publish_top_n = int(self.config.get("publish_top_n", 10))

        self.results: list[ScannerResult] = []
        self.excluded: dict[str, list[str]] = {}
        self.last_scanned_count = 0

    @property
    def universe(self) -> list[str]:
        return self.runtime.config.scanner.active_symbols

    async def run_cycle(self) -> None:
        config = self.runtime.config.scanner
        symbols = self.universe
        if not symbols:
            self.warn(f"universe '{config.active}' is empty; nothing to scan")
            return

        self.set_task(f"scanning {len(symbols)} symbols in '{config.active}'")

        try:
            results = await self.scan(symbols)
        except BrokerError as exc:
            self.warn(f"scan failed: {exc}")
            raise

        self.results = results
        self.last_scanned_count = len(symbols)
        # The agent's confidence is the top candidate's score: a quick signal
        # on the dashboard for whether anything interesting is happening.
        self.confidence = results[0].score if results else 0.0

        await self.publish(
            ScannerResultsEvent(
                results=results[: self.publish_top_n],
                universe=config.active,
                scanned_count=len(symbols),
                source=self.id,
            )
        )

        if results:
            top = ", ".join(f"{r.symbol}({r.score:.2f})" for r in results[:5])
            self.info(f"top candidates: {top}")
            self.set_task(f"{len(results)} candidates ranked, leader {results[0].symbol}")
        else:
            self.info("no candidates passed the filters")
            self.set_task("no candidates passed the filters")

    async def scan(self, symbols: list[str]) -> list[ScannerResult]:
        """One scan pass. Returns ranked candidates."""
        config = self.runtime.config.scanner
        indicator_config = config.indicators
        service = self.runtime.data_service

        # Two batched calls cover the whole universe.
        snapshots = await service.get_snapshots(symbols) if service else {}
        bars_by_symbol = (
            await service.get_bars_multi(
                symbols, timeframe=config.lookback.timeframe, limit=config.lookback.bars
            )
            if service
            else {}
        )

        candidates: list[ScannerResult] = []
        excluded: dict[str, list[str]] = {}

        for symbol in symbols:
            bars = bars_by_symbol.get(symbol, [])
            snapshot = snapshots.get(symbol)

            if not bars and snapshot is None:
                excluded[symbol] = ["no_data"]
                continue

            indicators = compute_indicators(
                symbol=symbol,
                bars=bars,
                timeframe=config.lookback.timeframe,
                ema_fast=indicator_config.ema_fast,
                ema_slow=indicator_config.ema_slow,
                sma_long=indicator_config.sma_long,
                rsi_period=indicator_config.rsi_period,
                atr_period=indicator_config.atr_period,
                bollinger_period=indicator_config.bollinger_period,
                bollinger_std=indicator_config.bollinger_std,
                momentum_period=indicator_config.momentum_period,
            )

            # Prefer the snapshot's price: it reflects the latest trade, while
            # the last bar close can be up to a full bar old.
            price = (snapshot.price if snapshot else None) or indicators.close
            percent_change = snapshot.percent_change_today if snapshot else None
            spread_percent = (
                snapshot.latest_quote.spread_pct if snapshot and snapshot.latest_quote else None
            )

            result = ScannerResult(
                symbol=symbol,
                price=price,
                percent_change=percent_change,
                volume=indicators.volume,
                relative_volume=indicators.relative_volume,
                atr=indicators.atr,
                atr_percent=indicators.atr_percent,
                volatility_pct=indicators.volatility_pct,
                spread_percent=spread_percent,
                distance_from_vwap_pct=indicators.distance_from_vwap_pct,
                rsi=indicators.rsi,
                momentum_pct=indicators.momentum_pct,
                ema_stack_bullish=indicators.ema_stack_bullish,
            )

            failed = apply_filters(result, config)
            if failed:
                result.failed_filters = failed
                excluded[symbol] = failed
                continue

            result.notes = describe(result)
            candidates.append(result)

        self.excluded = excluded
        if excluded:
            self.debug(f"{len(excluded)} symbols excluded by filters")

        return rank_candidates(candidates, config)

    def detail(self) -> dict[str, Any]:
        config = self.runtime.config.scanner
        return {
            "universe": config.active,
            "universe_size": len(self.universe),
            "scanned": self.last_scanned_count,
            "ranked": len(self.results),
            "excluded": len(self.excluded),
            "scan_interval_seconds": config.scan_interval_seconds,
            "weights": config.ranking.weights,
            "top": [
                {"symbol": r.symbol, "rank": r.rank, "score": r.score, "notes": r.notes}
                for r in self.results[:10]
            ],
        }
