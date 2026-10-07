"""EMA + VWAP momentum continuation. Long only.

**This strategy exists to prove the architecture works, not to make money.**

It has not been backtested. It has no demonstrated edge. Its parameters were
chosen because they are conventional, not because they were optimised — and
optimising them before there is an out-of-sample test would just be curve
fitting. Treat every number here as a placeholder.

What it does demonstrate, end to end:

    market data -> indicators -> signal -> TradeProposal
                -> Risk Agent -> Execution Agent -> paper order

The entry logic, in plain English: buy a liquid stock that is trending up on
the 5-minute chart, trading above today's VWAP, with above-average volume, and
that is not yet overbought. Risk is defined by a stop below the recent swing
low, and the position is sized so that hitting that stop costs a fixed small
percentage of the account.

Honest note on why this is hard: intraday momentum continuation is one of the
most heavily arbitraged patterns in the market. The reason to run it on paper
for months is to find out whether it clears costs — not to confirm that it
works.
"""

from __future__ import annotations

from datetime import timedelta

from app.config.schema import StrategyConfig
from app.models.enums import OrderType, Side, TimeInForce
from app.models.trading import Evidence, TradeProposal
from app.risk.rules import size_position
from app.strategies.base import Strategy, StrategyContext


class EmaVwapMomentumStrategy(Strategy):
    """Long-only intraday continuation."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        p = self.params
        self.rsi_max = float(p.get("rsi_max", 70.0))
        self.min_volume = float(p.get("min_volume", 500_000))
        self.min_relative_volume = float(p.get("min_relative_volume", 1.2))
        self.swing_lookback = int(p.get("swing_lookback", 10))
        self.stop_atr_buffer = float(p.get("stop_atr_buffer", 0.25))
        self.reward_risk_ratio = float(p.get("reward_risk_ratio", 2.0))
        self.max_risk_pct = float(config.risk.max_risk_per_trade_pct)

    def generate(self, context: StrategyContext) -> list[TradeProposal]:
        self.last_run_at = context.now
        proposals: list[TradeProposal] = []

        if not context.market_open:
            return proposals

        # Do not open a new intraday position close to the bell: there may not
        # be time for the thesis to play out, and an unmanaged overnight
        # position is a different trade from the one being proposed.
        close_buffer = self.config.risk.close_before_market_close_minutes * 60
        if context.seconds_until_close is not None and (context.seconds_until_close < close_buffer):
            return proposals

        for symbol in self.symbols:
            try:
                proposal = self._evaluate_symbol(symbol, context)
            except Exception as exc:
                # One bad symbol must not stop the others being evaluated.
                self.last_error = f"{symbol}: {exc}"
                self.log.warning("evaluation failed for %s: %s", symbol, exc)
                continue
            if proposal is not None:
                proposals.append(proposal)
                self.proposals_generated += 1

        return proposals

    def _evaluate_symbol(self, symbol: str, context: StrategyContext) -> TradeProposal | None:
        # Never add to an existing position: pyramiding changes the risk
        # profile of a trade that was already approved at a given size.
        if context.holds(symbol):
            return None

        indicators = context.indicators.get(symbol)
        if indicators is None or indicators.close is None:
            return None

        price = indicators.close
        evidence: list[Evidence] = []

        # ---- condition 1: trend alignment -------------------------------
        if indicators.ema_stack_bullish is not True:
            return None
        evidence.append(
            Evidence(
                source="trend",
                detail=(
                    f"price {price:.2f} > EMA20 {indicators.ema_fast:.2f} "
                    f"> EMA50 {indicators.ema_slow:.2f}"
                ),
                value=True,
                weight=1.0,
            )
        )

        # ---- condition 2: above session VWAP ----------------------------
        # VWAP is the day's volume-weighted reference. Buyers in control for
        # the session tend to keep price above it.
        if indicators.above_vwap is not True:
            return None
        evidence.append(
            Evidence(
                source="vwap",
                detail=(
                    f"price is {indicators.distance_from_vwap_pct:+.2f}% above "
                    f"session VWAP {indicators.vwap:.2f}"
                ),
                value=indicators.distance_from_vwap_pct,
                weight=1.0,
            )
        )

        # ---- condition 3: participation ---------------------------------
        # A move without volume is usually noise.
        if indicators.relative_volume is None or (
            indicators.relative_volume < self.min_relative_volume
        ):
            return None
        if indicators.average_volume is None or indicators.average_volume < self.min_volume:
            return None
        evidence.append(
            Evidence(
                source="volume",
                detail=(
                    f"relative volume {indicators.relative_volume:.2f}x "
                    f"(minimum {self.min_relative_volume:.2f}x)"
                ),
                value=indicators.relative_volume,
                weight=0.8,
            )
        )

        # ---- condition 4: momentum --------------------------------------
        if indicators.momentum_pct is None or indicators.momentum_pct <= 0:
            return None
        evidence.append(
            Evidence(
                source="momentum",
                detail=f"{indicators.momentum_pct:+.2f}% over the momentum lookback",
                value=indicators.momentum_pct,
                weight=0.8,
            )
        )

        # ---- condition 5: not already overbought ------------------------
        # Buying a vertical RSI print is buying the end of the move.
        if indicators.rsi is None or indicators.rsi > self.rsi_max:
            return None
        evidence.append(
            Evidence(
                source="rsi",
                detail=f"RSI {indicators.rsi:.1f} is below the {self.rsi_max:.0f} ceiling",
                value=indicators.rsi,
                weight=0.5,
            )
        )

        # ---- risk definition --------------------------------------------
        # The stop goes below the recent swing low, with an ATR buffer so that
        # ordinary noise does not take us out. Everything else is derived
        # from that distance.
        if indicators.swing_low is None or indicators.atr is None:
            return None

        stop_price = indicators.swing_low - (indicators.atr * self.stop_atr_buffer)
        if stop_price <= 0 or stop_price >= price:
            # Price is at or below its own swing low: no coherent stop exists.
            return None

        risk_per_share = price - stop_price
        target_price = price + (risk_per_share * self.reward_risk_ratio)

        quantity = size_position(
            equity=context.account.equity,
            entry_price=price,
            stop_price=stop_price,
            max_risk_pct=self.max_risk_pct,
            allow_fractional=False,
        )
        if quantity < 1:
            # Account too small for this stop distance. Not an error — the
            # correct outcome is simply no trade.
            return None

        evidence.append(
            Evidence(
                source="risk",
                detail=(
                    f"stop {stop_price:.2f} is {risk_per_share:.2f} below entry "
                    f"({risk_per_share / price * 100:.2f}%), sized {quantity:g} shares "
                    f"for {self.max_risk_pct:.2f}% account risk"
                ),
                value=risk_per_share,
                weight=1.0,
            )
        )

        # Confidence is a blend of the conditions that are a matter of degree
        # rather than pass/fail. It is a display and ranking aid, never an
        # input to position sizing — size comes from the stop distance alone.
        confidence = self._confidence(indicators)

        return TradeProposal(
            strategy_id=self.id,
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            entry_price=round(price, 2),
            stop_price=round(stop_price, 2),
            target_price=round(target_price, 2),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            confidence=confidence,
            evidence=evidence,
            invalidation=(
                f"Thesis fails if price closes back below the session VWAP "
                f"({indicators.vwap:.2f}) or breaks the swing low "
                f"({indicators.swing_low:.2f}). Hard stop at {stop_price:.2f}."
            ),
            created_at=context.now,
            # Short expiry: this is an intraday price-based setup, and a
            # proposal built on a five-minute-old price should not be filled.
            expires_at=context.now + timedelta(seconds=120),
        )

    def _confidence(self, indicators: object) -> float:
        """Blend the graded conditions into a 0-1 score."""
        relative_volume = getattr(indicators, "relative_volume", None) or 0.0
        momentum = getattr(indicators, "momentum_pct", None) or 0.0
        rsi = getattr(indicators, "rsi", None) or 50.0

        # Each component is normalised to 0-1, then averaged with weights.
        volume_score = min(1.0, max(0.0, (relative_volume - 1.0) / 2.0))
        momentum_score = min(1.0, max(0.0, momentum / 3.0))
        # RSI nearest 55-60 scores best: trending but with room to run.
        rsi_score = max(0.0, 1.0 - abs(rsi - 57.5) / 30.0)

        blended = volume_score * 0.4 + momentum_score * 0.35 + rsi_score * 0.25
        return round(min(0.95, max(0.05, blended)), 3)
