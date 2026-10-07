"""Technical Analysis Agent — multi-timeframe indicator and structure analysis.

Produces a `Signal` per symbol with structured `Evidence`.

Two deliberate choices:

*   **It follows the scanner.** Analysing every symbol on every timeframe
    every cycle would burn the rate limit for information nobody asked for.
    By default it analyses what the scanner surfaced.
*   **No indicator soup.** The brief was explicit, and it is the right
    instinct: twenty indicators that mostly measure the same thing produce
    confident-looking nonsense. This agent uses a small set of
    non-redundant reads — trend (EMA stack), location (VWAP), participation
    (relative volume), exhaustion (RSI) and volatility (ATR) — and records
    which ones actually contributed to the score.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.brokers.base import BrokerError
from app.events.types import ScannerResultsEvent, SignalEvent, Topics
from app.market_data.indicators import compute_indicators
from app.models.enums import SignalDirection
from app.models.market import IndicatorSet
from app.models.trading import Evidence, Signal

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime

#: Weight of each timeframe in the blended score. Higher timeframes dominate:
#: a 5-minute signal against the daily trend is a much worse bet than the
#: same signal with it.
_TIMEFRAME_WEIGHTS = {
    "5Min": 0.15,
    "15Min": 0.20,
    "1Hour": 0.30,
    "1Day": 0.35,
}


class TechnicalAgent(Agent):
    """Scores symbols across several timeframes."""

    agent_type = "technical"
    inputs = [Topics.SCANNER_RESULTS]
    outputs = [Topics.SIGNAL_TECHNICAL]
    tools = ["market_data.get_bars", "indicators"]
    subscriptions = [Topics.SCANNER_RESULTS]

    def __init__(self, runtime: AtlasRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        raw_timeframes = self.config.get("timeframes") or ["5Min", "15Min", "1Hour", "1Day"]
        self.timeframes = [str(t) for t in raw_timeframes]
        self.follow_scanner = bool(self.config.get("follow_scanner", True))

        self.signals: dict[str, Signal] = {}
        self.indicator_cache: dict[tuple[str, str], IndicatorSet] = {}
        self._watchlist: list[str] = []

    async def handle_event(self, event: Any) -> None:
        if not isinstance(event, ScannerResultsEvent) or not self.follow_scanner:
            return
        symbols = [r.symbol for r in event.results]
        if not symbols:
            return
        self._watchlist = symbols
        await self.analyse_many(symbols)

    async def analyse_many(self, symbols: list[str]) -> list[Signal]:
        self.set_task(f"analysing {len(symbols)} symbols on {len(self.timeframes)} timeframes")
        signals: list[Signal] = []
        for symbol in symbols:
            try:
                signal = await self.analyse(symbol)
            except BrokerError as exc:
                self.warn(f"analysis failed for {symbol}: {exc}")
                continue
            if signal is not None:
                signals.append(signal)
        if signals:
            best = max(signals, key=lambda s: s.confidence)
            self.confidence = best.confidence
            self.set_task(
                f"{len(signals)} signals, strongest {best.symbol} "
                f"{best.direction.value} ({best.confidence:.0%})"
            )
        return signals

    async def analyse(self, symbol: str) -> Signal | None:
        """Compute indicators per timeframe and blend into one signal."""
        service = self.runtime.data_service
        if service is None:
            return None

        scanner_config = self.runtime.config.scanner.indicators
        per_timeframe: dict[str, IndicatorSet] = {}

        for timeframe in self.timeframes:
            bars = await service.get_bars(symbol, timeframe=timeframe, limit=200)
            if len(bars) < 2:
                continue
            indicators = compute_indicators(
                symbol=symbol,
                bars=bars,
                timeframe=timeframe,
                ema_fast=scanner_config.ema_fast,
                ema_slow=scanner_config.ema_slow,
                sma_long=scanner_config.sma_long,
                rsi_period=scanner_config.rsi_period,
                atr_period=scanner_config.atr_period,
                bollinger_period=scanner_config.bollinger_period,
                bollinger_std=scanner_config.bollinger_std,
                momentum_period=scanner_config.momentum_period,
            )
            per_timeframe[timeframe] = indicators
            self.indicator_cache[(symbol, timeframe)] = indicators

        if not per_timeframe:
            return None

        score, evidence, risk_flags = self._score(symbol, per_timeframe)

        # Map a -1..+1 score onto a direction with a neutral dead zone. A
        # weakly positive reading is not a bullish signal, it is noise.
        if score >= 0.25:
            direction = SignalDirection.BULLISH
        elif score <= -0.25:
            direction = SignalDirection.BEARISH
        else:
            direction = SignalDirection.NEUTRAL

        signal = Signal(
            agent_id=self.id,
            symbol=symbol,
            direction=direction,
            confidence=round(min(1.0, abs(score)), 3),
            timeframe="multi",
            evidence=evidence,
            risk_flags=risk_flags,
            score=round((score + 1) * 50, 1),  # 0-100 for display
            notes=(
                f"blended across {', '.join(per_timeframe)} "
                f"(higher timeframes weighted more heavily)"
            ),
        )
        self.signals[symbol] = signal
        await self.publish(SignalEvent(signal=signal, source=self.id))
        return signal

    def _score(
        self, symbol: str, per_timeframe: dict[str, IndicatorSet]
    ) -> tuple[float, list[Evidence], list[str]]:
        """Blend timeframes into a single -1..+1 score.

        Only indicators that actually read one way or the other contribute,
        and each contribution is recorded as evidence, so a score can always
        be explained rather than merely reported.
        """
        evidence: list[Evidence] = []
        risk_flags: list[str] = []
        weighted_total = 0.0
        weight_used = 0.0

        for timeframe, indicators in per_timeframe.items():
            weight = _TIMEFRAME_WEIGHTS.get(timeframe, 0.2)
            components: list[float] = []

            # --- trend: EMA stack ---------------------------------------
            if indicators.ema_stack_bullish is not None:
                value = 1.0 if indicators.ema_stack_bullish else -1.0
                components.append(value)
                evidence.append(
                    Evidence(
                        source=f"{timeframe}:trend",
                        detail=(
                            "price above both EMAs"
                            if indicators.ema_stack_bullish
                            else "price below the EMA stack"
                        ),
                        value=value,
                        weight=weight,
                    )
                )

            # --- location: VWAP ------------------------------------------
            if indicators.above_vwap is not None:
                value = 1.0 if indicators.above_vwap else -1.0
                components.append(value)
                evidence.append(
                    Evidence(
                        source=f"{timeframe}:vwap",
                        detail=(
                            f"{indicators.distance_from_vwap_pct:+.2f}% versus VWAP"
                            if indicators.distance_from_vwap_pct is not None
                            else "relative to VWAP"
                        ),
                        value=indicators.distance_from_vwap_pct,
                        weight=weight * 0.8,
                    )
                )

            # --- momentum -------------------------------------------------
            if indicators.momentum_pct is not None:
                # Saturate at +/-3%: beyond that the reading is "strong", and
                # scaling further would let one timeframe swamp the blend.
                value = max(-1.0, min(1.0, indicators.momentum_pct / 3.0))
                components.append(value)
                evidence.append(
                    Evidence(
                        source=f"{timeframe}:momentum",
                        detail=f"{indicators.momentum_pct:+.2f}% over the lookback",
                        value=indicators.momentum_pct,
                        weight=weight * 0.8,
                    )
                )

            # --- exhaustion: RSI -----------------------------------------
            if indicators.rsi is not None:
                if indicators.rsi >= 70:
                    components.append(-0.5)
                    risk_flags.append(f"{timeframe}_overbought_rsi_{indicators.rsi:.0f}")
                    evidence.append(
                        Evidence(
                            source=f"{timeframe}:rsi",
                            detail=f"RSI {indicators.rsi:.0f}: overbought, chase risk",
                            value=indicators.rsi,
                            weight=weight * 0.5,
                        )
                    )
                elif indicators.rsi <= 30:
                    components.append(0.5)
                    risk_flags.append(f"{timeframe}_oversold_rsi_{indicators.rsi:.0f}")
                    evidence.append(
                        Evidence(
                            source=f"{timeframe}:rsi",
                            detail=f"RSI {indicators.rsi:.0f}: oversold",
                            value=indicators.rsi,
                            weight=weight * 0.5,
                        )
                    )

            # --- volatility is a risk note, not a direction ---------------
            if indicators.atr_percent is not None and indicators.atr_percent > 4.0:
                risk_flags.append(
                    f"{timeframe}_high_volatility_atr_{indicators.atr_percent:.1f}pct"
                )

            if components:
                weighted_total += (sum(components) / len(components)) * weight
                weight_used += weight

        if weight_used <= 0:
            return 0.0, evidence, risk_flags

        return weighted_total / weight_used, evidence, risk_flags

    def detail(self) -> dict[str, Any]:
        return {
            "timeframes": self.timeframes,
            "follow_scanner": self.follow_scanner,
            "watchlist": self._watchlist,
            "signals": [
                {
                    "symbol": s.symbol,
                    "direction": s.direction.value,
                    "confidence": s.confidence,
                    "score": s.score,
                    "risk_flags": s.risk_flags,
                }
                for s in sorted(self.signals.values(), key=lambda s: s.confidence, reverse=True)[
                    :10
                ]
            ],
        }
