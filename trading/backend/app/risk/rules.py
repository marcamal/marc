"""Deterministic risk rules. The Risk Agent's veto lives here.

Three properties this module is built to guarantee:

1.  **No IO, no randomness, no clock reads beyond an injected `now`.** The
    engine is a pure function of `(proposal, context)`. That is what makes it
    exhaustively testable — and tests that *try to bypass it* are part of the
    suite.

2.  **No AI anywhere near it.** An LLM may propose a trade and may explain a
    rejection, but it cannot evaluate one. Risk is arithmetic and arithmetic
    does not need a language model. There is no prompt, no model call and no
    override path in this file.

3.  **Hard ceilings above the config.** `risk.yaml` can only ever make ATLAS
    *more* conservative. The `ABSOLUTE_*` constants below are compiled-in
    upper bounds: a typo (or a careless edit) that sets
    `max_risk_per_trade_pct: 50` is clamped to 5%, loudly. Config is input,
    not authority.

Every rule produces a `RiskCheckResult` with the limit and the observed value,
so a rejection is self-documenting and the Mentor Agent can teach from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.brokers.base import BrokerCapabilities
from app.config.schema import RiskConfig, StrategyRiskConfig
from app.config.settings import TradingMode
from app.core.logging import get_logger
from app.models.enums import AssetClass, RiskDecisionType, Side
from app.models.market import MarketClock, Quote
from app.models.trading import (
    AccountSnapshot,
    PortfolioSnapshot,
    RiskCheckResult,
    RiskDecision,
    TradeProposal,
)

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Compiled-in ceilings. Configuration cannot exceed these, ever.
# --------------------------------------------------------------------------- #

#: No single trade may risk more than this share of equity, whatever the YAML says.
ABSOLUTE_MAX_RISK_PER_TRADE_PCT = 5.0
#: No single position may exceed this share of equity.
ABSOLUTE_MAX_POSITION_PCT = 25.0
#: Total exposure ceiling. Above 100% implies margin.
ABSOLUTE_MAX_TOTAL_EXPOSURE_PCT = 100.0
#: Leverage ceiling. 1.0 means cash-account behaviour.
ABSOLUTE_MAX_LEVERAGE = 2.0
#: Daily loss ceiling before everything stops.
ABSOLUTE_MAX_DAILY_LOSS_PCT = 10.0
#: Hard cap on orders per day, regardless of config.
ABSOLUTE_MAX_ORDERS_PER_DAY = 100


def _clamp(value: float, ceiling: float, name: str) -> float:
    """Apply a compiled-in ceiling, complaining if it bites."""
    if value > ceiling:
        log.error(
            "risk config exceeds a hard-coded ceiling and was clamped",
            extra={"setting": name, "configured": value, "clamped_to": ceiling},
        )
        return ceiling
    return value


@dataclass
class RiskContext:
    """Everything the engine needs to judge one proposal.

    Assembled by the Risk Agent (which does the IO) and passed in whole, so
    the engine itself stays pure.
    """

    account: AccountSnapshot
    portfolio: PortfolioSnapshot
    config: RiskConfig
    mode: TradingMode
    capabilities: BrokerCapabilities

    clock: MarketClock | None = None
    quote: Quote | None = None
    asset_class: AssetClass = AssetClass.US_EQUITY

    kill_switch_engaged: bool = False
    kill_switch_reason: str | None = None

    #: Orders already submitted today, in total and per symbol.
    orders_today: int = 0
    orders_today_by_symbol: dict[str, int] = field(default_factory=dict)
    #: Open positions and orders opened by the proposing strategy.
    strategy_open_positions: int = 0
    strategy_orders_today: int = 0
    #: Per-strategy overrides from strategies.yaml.
    strategy_risk: StrategyRiskConfig | None = None

    #: Average daily volume for the liquidity gate, when known.
    average_volume: float | None = None

    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def effective_max_risk_pct(self) -> float:
        """Stricter of the global and strategy limits, under the hard ceiling."""
        limit = _clamp(
            self.config.max_risk_per_trade_pct,
            ABSOLUTE_MAX_RISK_PER_TRADE_PCT,
            "max_risk_per_trade_pct",
        )
        if self.strategy_risk is not None:
            limit = min(limit, self.strategy_risk.max_risk_per_trade_pct)
        return limit

    def effective_max_orders_per_day(self) -> int:
        limit = int(
            _clamp(
                float(self.config.max_orders_per_day),
                float(ABSOLUTE_MAX_ORDERS_PER_DAY),
                "max_orders_per_day",
            )
        )
        return limit


class RiskEngine:
    """Evaluates trade proposals. Its verdict binds the Execution Agent."""

    def evaluate(self, proposal: TradeProposal, context: RiskContext) -> RiskDecision:
        """Run every rule and return a binding decision.

        All rules run even after the first failure. A partial audit trail would
        make the Mentor Agent's explanation misleading ("rejected for X" when
        Y and Z also failed), and the extra work is a few dozen comparisons.
        """
        checks: list[RiskCheckResult] = []

        checks.extend(self._check_blocking_conditions(proposal, context))
        checks.extend(self._check_proposal_hygiene(proposal, context))
        checks.extend(self._check_market_conditions(proposal, context))
        checks.extend(self._check_capability(proposal, context))
        checks.extend(self._check_sizing(proposal, context))
        checks.extend(self._check_portfolio_limits(proposal, context))
        checks.extend(self._check_rate_limits(proposal, context))
        checks.extend(self._check_account_health(proposal, context))

        failed = [c for c in checks if not c.passed]

        if failed:
            decision = RiskDecision(
                proposal_id=proposal.id,
                decision=RiskDecisionType.REJECTED,
                checks=checks,
                reasons=[f"{c.rule}: {c.detail}" for c in failed],
                trace_id=proposal.trace_id,
            )
            log.warning(
                "RISK VETO",
                extra={
                    "proposal_id": proposal.id,
                    "symbol": proposal.symbol,
                    "strategy": proposal.strategy_id,
                    "failed_rules": [c.rule for c in failed],
                },
            )
            return decision

        # Everything passed at the requested size. Now see whether a smaller
        # size would still be needed — it will not be, since the sizing checks
        # above already passed, but the helper also handles the case where a
        # cap allows *some* quantity and the caller asked us to downsize.
        max_quantity = self._max_allowed_quantity(proposal, context)
        if max_quantity is not None and max_quantity < proposal.quantity:
            if max_quantity < 1:
                return RiskDecision(
                    proposal_id=proposal.id,
                    decision=RiskDecisionType.REJECTED,
                    checks=checks,
                    reasons=["position_sizing: no whole share fits inside the risk limits"],
                    trace_id=proposal.trace_id,
                )
            log.info(
                "risk approved with reduced size",
                extra={
                    "proposal_id": proposal.id,
                    "symbol": proposal.symbol,
                    "requested": proposal.quantity,
                    "approved": max_quantity,
                },
            )
            return RiskDecision(
                proposal_id=proposal.id,
                decision=RiskDecisionType.APPROVED_REDUCED,
                checks=checks,
                approved_quantity=max_quantity,
                reasons=[
                    f"quantity reduced from {proposal.quantity:g} to {max_quantity:g} "
                    f"to stay inside risk limits"
                ],
                trace_id=proposal.trace_id,
            )

        log.info(
            "risk approved",
            extra={
                "proposal_id": proposal.id,
                "symbol": proposal.symbol,
                "qty": proposal.quantity,
                "risk": round(proposal.estimated_risk, 2),
            },
        )
        return RiskDecision(
            proposal_id=proposal.id,
            decision=RiskDecisionType.APPROVED,
            checks=checks,
            approved_quantity=proposal.quantity,
            trace_id=proposal.trace_id,
        )

    # ------------------------------------------------------------------ #
    # rule groups
    # ------------------------------------------------------------------ #

    def _check_blocking_conditions(
        self, proposal: TradeProposal, context: RiskContext
    ) -> list[RiskCheckResult]:
        """Absolute blocks: kill switch, trading mode, broker restrictions."""
        checks = [
            RiskCheckResult(
                rule="kill_switch",
                passed=not context.kill_switch_engaged,
                detail=(
                    f"kill switch is ENGAGED ({context.kill_switch_reason or 'no reason recorded'})"
                    if context.kill_switch_engaged
                    else "kill switch clear"
                ),
            )
        ]

        # BACKTEST and REPLAY must never reach a broker. If a proposal gets
        # this far in those modes, something is wired wrong.
        mode_ok = context.mode in (TradingMode.PAPER, TradingMode.LIVE)
        checks.append(
            RiskCheckResult(
                rule="trading_mode",
                passed=mode_ok,
                detail=(
                    f"mode {context.mode.value} does not place broker orders"
                    if not mode_ok
                    else f"mode {context.mode.value}"
                ),
            )
        )

        checks.append(
            RiskCheckResult(
                rule="account_not_blocked",
                passed=not context.account.is_restricted,
                detail=(
                    "broker reports trading or account blocked"
                    if context.account.is_restricted
                    else "account active"
                ),
            )
        )
        return checks

    def _check_proposal_hygiene(
        self, proposal: TradeProposal, context: RiskContext
    ) -> list[RiskCheckResult]:
        """Structural validation, independent of market conditions."""
        checks: list[RiskCheckResult] = []

        # The configured required-field list. Pydantic already enforces most
        # of this at construction, but the config is the operator's contract
        # and a proposal built by a future code path must still satisfy it.
        missing = [
            name
            for name in context.config.required_proposal_fields
            if getattr(proposal, name, None) in (None, "", 0)
        ]
        checks.append(
            RiskCheckResult(
                rule="required_fields",
                passed=not missing,
                detail=f"missing required fields: {missing}" if missing else "all present",
            )
        )

        age = proposal.age_seconds(context.now)
        max_age = context.config.max_proposal_age_seconds
        checks.append(
            RiskCheckResult(
                rule="proposal_freshness",
                passed=age <= max_age,
                detail=f"proposal is {age:.0f}s old (limit {max_age}s)",
                limit=float(max_age),
                observed=age,
            )
        )

        expired = proposal.is_expired(context.now)
        checks.append(
            RiskCheckResult(
                rule="proposal_not_expired",
                passed=not expired,
                detail="proposal has passed its own expiry" if expired else "within expiry",
            )
        )

        # Stop must be a real distance away, or position sizing divides by ~0
        # and produces an enormous quantity.
        risk_per_share = proposal.risk_per_share
        meaningful = risk_per_share > 0 and (
            proposal.entry_price <= 0 or risk_per_share / proposal.entry_price >= 0.0005
        )
        checks.append(
            RiskCheckResult(
                rule="stop_distance",
                passed=meaningful,
                detail=(
                    f"stop is {risk_per_share:.4f} from entry "
                    f"({(risk_per_share / proposal.entry_price * 100) if proposal.entry_price else 0:.3f}%), "
                    f"too close to size safely"
                    if not meaningful
                    else f"stop {risk_per_share:.4f} from entry"
                ),
                observed=risk_per_share,
            )
        )

        ratio = proposal.reward_risk_ratio
        min_ratio = context.config.min_reward_risk_ratio
        if ratio is None:
            # No target set. Permitted, but recorded: a trade without a target
            # cannot be assessed for reward/risk.
            checks.append(
                RiskCheckResult(
                    rule="reward_risk_ratio",
                    passed=True,
                    detail="no target price set, reward/risk not assessed",
                    limit=min_ratio,
                )
            )
        else:
            checks.append(
                RiskCheckResult(
                    rule="reward_risk_ratio",
                    passed=ratio >= min_ratio,
                    detail=f"reward/risk {ratio:.2f} (minimum {min_ratio:.2f})",
                    limit=min_ratio,
                    observed=ratio,
                )
            )

        return checks

    def _check_market_conditions(
        self, proposal: TradeProposal, context: RiskContext
    ) -> list[RiskCheckResult]:
        """Is this market currently fit to trade?"""
        checks: list[RiskCheckResult] = []

        if context.config.require_market_open:
            is_open = context.clock.is_open if context.clock else False
            checks.append(
                RiskCheckResult(
                    rule="market_open",
                    passed=is_open,
                    detail=(
                        "market is closed"
                        if context.clock
                        else "market clock unavailable, refusing to assume open"
                    )
                    if not is_open
                    else "market open",
                )
            )

        # Stale data is a hard stop. Trading on a price that stopped updating
        # is the failure mode that turns an outage into a loss.
        quote = context.quote
        max_age = float(context.config.max_data_staleness_seconds)
        if quote is None:
            checks.append(
                RiskCheckResult(
                    rule="data_freshness",
                    passed=False,
                    detail="no quote available for this symbol",
                    limit=max_age,
                )
            )
        else:
            age = quote.age_seconds
            checks.append(
                RiskCheckResult(
                    rule="data_freshness",
                    passed=age <= max_age,
                    detail=f"quote is {age:.1f}s old (limit {max_age:.0f}s)",
                    limit=max_age,
                    observed=age,
                )
            )

            # A one-sided book has no meaningful spread; treat it as untradeable
            # rather than as a zero spread, which would pass the check.
            if quote.bid_price <= 0 or quote.ask_price <= 0:
                checks.append(
                    RiskCheckResult(
                        rule="spread",
                        passed=False,
                        detail="one-sided or empty order book",
                    )
                )
            else:
                spread_pct = quote.spread_pct
                checks.append(
                    RiskCheckResult(
                        rule="spread",
                        passed=spread_pct <= context.config.max_spread_pct,
                        detail=(
                            f"spread {spread_pct:.3f}% (limit {context.config.max_spread_pct:.3f}%)"
                        ),
                        limit=context.config.max_spread_pct,
                        observed=spread_pct,
                    )
                )

        if context.average_volume is not None:
            checks.append(
                RiskCheckResult(
                    rule="liquidity",
                    passed=context.average_volume >= context.config.min_average_volume,
                    detail=(
                        f"average volume {context.average_volume:,.0f} "
                        f"(minimum {context.config.min_average_volume:,})"
                    ),
                    limit=float(context.config.min_average_volume),
                    observed=context.average_volume,
                )
            )

        return checks

    def _check_capability(
        self, proposal: TradeProposal, context: RiskContext
    ) -> list[RiskCheckResult]:
        """Can this account actually trade this instrument this way?

        Rather than assume what is available in the operator's jurisdiction,
        ATLAS asks the broker and refuses what is not enabled.
        """
        checks: list[RiskCheckResult] = []
        caps = context.capabilities

        supported = context.asset_class is not AssetClass.UNSUPPORTED
        if context.asset_class is AssetClass.CRYPTO and not caps.supports_crypto:
            supported = False
        if context.asset_class is AssetClass.US_OPTION and not caps.supports_options:
            supported = False

        checks.append(
            RiskCheckResult(
                rule="asset_supported",
                passed=supported,
                detail=(
                    f"{proposal.symbol} ({context.asset_class.value}) is not tradable "
                    f"on this account — UNSUPPORTED BY CURRENT BROKER"
                    if not supported
                    else f"{context.asset_class.value} supported"
                ),
            )
        )

        # A sell with no position is a short. On a cash account that is simply
        # not possible, and a broker rejection is a worse way to find out.
        if proposal.side is Side.SELL:
            held = next(
                (p for p in context.portfolio.positions if p.symbol == proposal.symbol), None
            )
            held_quantity = held.quantity if held else 0.0
            would_short = proposal.quantity > held_quantity
            checks.append(
                RiskCheckResult(
                    rule="short_selling",
                    passed=(not would_short) or caps.supports_short_selling,
                    detail=(
                        f"selling {proposal.quantity:g} but only {held_quantity:g} held, "
                        f"and shorting is not enabled on this account"
                        if would_short and not caps.supports_short_selling
                        else "sell is covered by the existing position"
                    ),
                )
            )

        # Fractional quantities need explicit broker support.
        if proposal.quantity != int(proposal.quantity):
            checks.append(
                RiskCheckResult(
                    rule="fractional_shares",
                    passed=caps.supports_fractional_shares,
                    detail=(
                        f"quantity {proposal.quantity} is fractional and this account "
                        f"does not support fractional shares"
                        if not caps.supports_fractional_shares
                        else "fractional shares supported"
                    ),
                )
            )

        return checks

    def _check_sizing(self, proposal: TradeProposal, context: RiskContext) -> list[RiskCheckResult]:
        """Per-trade size and risk."""
        checks: list[RiskCheckResult] = []
        equity = context.account.equity

        if equity <= 0:
            return [
                RiskCheckResult(
                    rule="equity_available",
                    passed=False,
                    detail=f"account equity is {equity}, cannot size a position",
                    observed=equity,
                )
            ]

        # --- risk per trade ---------------------------------------------
        risk_pct = (proposal.estimated_risk / equity) * 100
        max_risk_pct = context.effective_max_risk_pct()
        checks.append(
            RiskCheckResult(
                rule="max_risk_per_trade",
                passed=risk_pct <= max_risk_pct,
                detail=(
                    f"risks {risk_pct:.3f}% of equity "
                    f"(${proposal.estimated_risk:,.2f}), limit {max_risk_pct:.3f}%"
                ),
                limit=max_risk_pct,
                observed=risk_pct,
            )
        )

        # --- position size ----------------------------------------------
        position_pct = (proposal.notional / equity) * 100
        max_position_pct = _clamp(
            context.config.max_position_pct, ABSOLUTE_MAX_POSITION_PCT, "max_position_pct"
        )
        checks.append(
            RiskCheckResult(
                rule="max_position_size",
                passed=position_pct <= max_position_pct,
                detail=(
                    f"position is {position_pct:.2f}% of equity "
                    f"(${proposal.notional:,.2f}), limit {max_position_pct:.2f}%"
                ),
                limit=max_position_pct,
                observed=position_pct,
            )
        )

        # --- absolute notional bounds -----------------------------------
        checks.append(
            RiskCheckResult(
                rule="max_order_notional",
                passed=proposal.notional <= context.config.max_order_notional,
                detail=(
                    f"order notional ${proposal.notional:,.2f} "
                    f"(limit ${context.config.max_order_notional:,.2f})"
                ),
                limit=context.config.max_order_notional,
                observed=proposal.notional,
            )
        )
        meets_minimum = proposal.notional >= context.config.min_order_notional
        checks.append(
            RiskCheckResult(
                rule="min_order_notional",
                passed=meets_minimum,
                detail=(
                    f"order notional ${proposal.notional:,.2f} is below the "
                    f"${context.config.min_order_notional:,.2f} minimum"
                    if not meets_minimum
                    else f"order notional ${proposal.notional:,.2f} "
                    f"(minimum ${context.config.min_order_notional:,.2f})"
                ),
                limit=context.config.min_order_notional,
                observed=proposal.notional,
            )
        )

        # --- buying power ------------------------------------------------
        if proposal.side is Side.BUY:
            checks.append(
                RiskCheckResult(
                    rule="buying_power",
                    passed=proposal.notional <= context.account.buying_power,
                    detail=(
                        f"needs ${proposal.notional:,.2f}, "
                        f"buying power ${context.account.buying_power:,.2f}"
                    ),
                    limit=context.account.buying_power,
                    observed=proposal.notional,
                )
            )

        return checks

    def _check_portfolio_limits(
        self, proposal: TradeProposal, context: RiskContext
    ) -> list[RiskCheckResult]:
        """Book-level exposure and concentration."""
        checks: list[RiskCheckResult] = []
        equity = context.account.equity
        if equity <= 0:
            return checks

        portfolio = context.portfolio
        existing_symbols = {p.symbol for p in portfolio.positions}
        opens_new_position = proposal.symbol not in existing_symbols

        # --- position count ----------------------------------------------
        if opens_new_position and proposal.side is Side.BUY:
            projected_count = portfolio.open_position_count + 1
            checks.append(
                RiskCheckResult(
                    rule="max_open_positions",
                    passed=projected_count <= context.config.max_open_positions,
                    detail=(
                        f"would hold {projected_count} positions "
                        f"(limit {context.config.max_open_positions})"
                    ),
                    limit=float(context.config.max_open_positions),
                    observed=float(projected_count),
                )
            )

            if context.strategy_risk is not None:
                projected_strategy = context.strategy_open_positions + 1
                checks.append(
                    RiskCheckResult(
                        rule="strategy_max_concurrent_positions",
                        passed=projected_strategy <= context.strategy_risk.max_concurrent_positions,
                        detail=(
                            f"strategy {proposal.strategy_id} would hold "
                            f"{projected_strategy} positions "
                            f"(limit {context.strategy_risk.max_concurrent_positions})"
                        ),
                        limit=float(context.strategy_risk.max_concurrent_positions),
                        observed=float(projected_strategy),
                    )
                )

        # --- total exposure ----------------------------------------------
        if proposal.side is Side.BUY:
            projected_exposure = portfolio.total_exposure + proposal.notional
            projected_pct = (projected_exposure / equity) * 100
            max_exposure_pct = _clamp(
                context.config.max_total_exposure_pct,
                ABSOLUTE_MAX_TOTAL_EXPOSURE_PCT,
                "max_total_exposure_pct",
            )
            checks.append(
                RiskCheckResult(
                    rule="max_total_exposure",
                    passed=projected_pct <= max_exposure_pct,
                    detail=(
                        f"total exposure would be {projected_pct:.2f}% of equity "
                        f"(limit {max_exposure_pct:.2f}%)"
                    ),
                    limit=max_exposure_pct,
                    observed=projected_pct,
                )
            )

            # --- correlated exposure -------------------------------------
            group = context.config.correlation_group_for(proposal.symbol)
            if group:
                current = portfolio.exposure_by_correlation_group.get(group, 0.0)
                projected_group_pct = ((current + proposal.notional) / equity) * 100
                checks.append(
                    RiskCheckResult(
                        rule="max_correlated_exposure",
                        passed=projected_group_pct <= context.config.max_correlated_exposure_pct,
                        detail=(
                            f"correlation group '{group}' would be "
                            f"{projected_group_pct:.2f}% of equity "
                            f"(limit {context.config.max_correlated_exposure_pct:.2f}%)"
                        ),
                        limit=context.config.max_correlated_exposure_pct,
                        observed=projected_group_pct,
                    )
                )
            else:
                # Honest about what we cannot assess yet. A real sector
                # classification arrives with the fundamental data layer.
                checks.append(
                    RiskCheckResult(
                        rule="max_correlated_exposure",
                        passed=True,
                        detail=(
                            f"{proposal.symbol} is in no correlation group in risk.yaml; "
                            f"correlated exposure not assessed"
                        ),
                    )
                )

        # --- leverage ----------------------------------------------------
        max_leverage = _clamp(context.config.max_leverage, ABSOLUTE_MAX_LEVERAGE, "max_leverage")
        # Never permit more leverage than the account itself offers.
        max_leverage = min(max_leverage, max(1.0, context.capabilities.max_leverage))
        projected_leverage = (
            portfolio.total_exposure + (proposal.notional if proposal.side is Side.BUY else 0)
        ) / equity
        checks.append(
            RiskCheckResult(
                rule="max_leverage",
                passed=projected_leverage <= max_leverage + 1e-9,
                detail=f"leverage would be {projected_leverage:.2f}x (limit {max_leverage:.2f}x)",
                limit=max_leverage,
                observed=projected_leverage,
            )
        )

        return checks

    def _check_rate_limits(
        self, proposal: TradeProposal, context: RiskContext
    ) -> list[RiskCheckResult]:
        """Circuit breakers against runaway loops."""
        checks: list[RiskCheckResult] = []

        max_orders = context.effective_max_orders_per_day()
        checks.append(
            RiskCheckResult(
                rule="max_orders_per_day",
                passed=context.orders_today < max_orders,
                detail=f"{context.orders_today} orders submitted today (limit {max_orders})",
                limit=float(max_orders),
                observed=float(context.orders_today),
            )
        )

        symbol_count = context.orders_today_by_symbol.get(proposal.symbol, 0)
        checks.append(
            RiskCheckResult(
                rule="max_orders_per_symbol_per_day",
                passed=symbol_count < context.config.max_orders_per_symbol_per_day,
                detail=(
                    f"{symbol_count} orders for {proposal.symbol} today "
                    f"(limit {context.config.max_orders_per_symbol_per_day})"
                ),
                limit=float(context.config.max_orders_per_symbol_per_day),
                observed=float(symbol_count),
            )
        )

        if context.strategy_risk is not None:
            checks.append(
                RiskCheckResult(
                    rule="strategy_max_orders_per_day",
                    passed=context.strategy_orders_today < context.strategy_risk.max_orders_per_day,
                    detail=(
                        f"strategy {proposal.strategy_id} has submitted "
                        f"{context.strategy_orders_today} orders today "
                        f"(limit {context.strategy_risk.max_orders_per_day})"
                    ),
                    limit=float(context.strategy_risk.max_orders_per_day),
                    observed=float(context.strategy_orders_today),
                )
            )

        return checks

    def _check_account_health(
        self, proposal: TradeProposal, context: RiskContext
    ) -> list[RiskCheckResult]:
        """Daily loss and drawdown gates."""
        checks: list[RiskCheckResult] = []
        portfolio = context.portfolio

        max_daily_loss = _clamp(
            context.config.max_daily_loss_pct,
            ABSOLUTE_MAX_DAILY_LOSS_PCT,
            "max_daily_loss_pct",
        )
        # daily_pl_pct is negative on a losing day; compare the magnitude.
        daily_loss_pct = -min(0.0, portfolio.daily_pl_pct)
        checks.append(
            RiskCheckResult(
                rule="max_daily_loss",
                passed=daily_loss_pct < max_daily_loss,
                detail=(
                    f"down {daily_loss_pct:.2f}% today (limit {max_daily_loss:.2f}%)"
                    if daily_loss_pct > 0
                    else f"daily P&L {portfolio.daily_pl_pct:+.2f}%"
                ),
                limit=max_daily_loss,
                observed=daily_loss_pct,
            )
        )

        checks.append(
            RiskCheckResult(
                rule="max_portfolio_drawdown",
                passed=portfolio.drawdown_pct < context.config.max_portfolio_drawdown_pct,
                detail=(
                    f"drawdown {portfolio.drawdown_pct:.2f}% from the high water mark "
                    f"(limit {context.config.max_portfolio_drawdown_pct:.2f}%)"
                ),
                limit=context.config.max_portfolio_drawdown_pct,
                observed=portfolio.drawdown_pct,
            )
        )

        # A mismatch between our books and the broker's means we do not
        # reliably know what we hold. Sizing decisions cannot be trusted.
        checks.append(
            RiskCheckResult(
                rule="broker_reconciled",
                passed=not portfolio.reconciliation_mismatch,
                detail=(
                    "ATLAS and the broker disagree about positions; "
                    f"{'; '.join(portfolio.mismatch_detail[:3])}"
                    if portfolio.reconciliation_mismatch
                    else "positions reconciled with broker"
                ),
            )
        )

        return checks

    # ------------------------------------------------------------------ #
    # sizing helper
    # ------------------------------------------------------------------ #

    def _max_allowed_quantity(self, proposal: TradeProposal, context: RiskContext) -> float | None:
        """Largest quantity that satisfies every size-based cap.

        Returns `None` when no cap applies. Always floors to a whole share
        unless the broker supports fractional quantities, because rounding up
        would breach the very limit we are respecting.
        """
        equity = context.account.equity
        if equity <= 0 or proposal.entry_price <= 0:
            return None

        caps: list[float] = []

        risk_per_share = proposal.risk_per_share
        if risk_per_share > 0:
            risk_budget = equity * (context.effective_max_risk_pct() / 100)
            caps.append(risk_budget / risk_per_share)

        max_position_pct = _clamp(
            context.config.max_position_pct, ABSOLUTE_MAX_POSITION_PCT, "max_position_pct"
        )
        caps.append((equity * (max_position_pct / 100)) / proposal.entry_price)
        caps.append(context.config.max_order_notional / proposal.entry_price)

        if proposal.side is Side.BUY:
            caps.append(context.account.buying_power / proposal.entry_price)

        if not caps:
            return None

        allowed = min(caps)
        if not context.capabilities.supports_fractional_shares:
            allowed = float(int(allowed))
        return max(0.0, allowed)


def size_position(
    equity: float,
    entry_price: float,
    stop_price: float,
    max_risk_pct: float,
    allow_fractional: bool = False,
) -> float:
    """Position size from a risk budget — the canonical formula.

        quantity = (equity * max_risk_pct / 100) / |entry - stop|

    Exposed separately so strategies size trades the same way the Risk Agent
    will judge them, which means far fewer proposals get rejected or
    downsized. Returns 0.0 when no valid size exists.
    """
    if equity <= 0 or entry_price <= 0:
        return 0.0
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        return 0.0

    risk_budget = equity * (max_risk_pct / 100.0)
    quantity = risk_budget / risk_per_share

    if not allow_fractional:
        quantity = float(int(quantity))
    return max(0.0, quantity)
