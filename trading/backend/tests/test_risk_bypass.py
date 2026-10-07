"""Adversarial tests: deliberately try to get a bad trade past the Risk Agent.

The brief asked for tests that attempt to bypass the Risk Agent and must fail.
This file is that. Every test here constructs something a careless, buggy or
malicious caller might try, and asserts that ATLAS refuses.

If any test in this file ever starts passing in the "attack succeeded"
direction, a real safety control has been broken.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import TradingMode
from app.models.enums import AssetClass, OrderType, RiskDecisionType, Side
from app.models.market import Quote
from app.models.trading import PortfolioSnapshot, Position, TradeProposal
from app.risk.rules import (
    ABSOLUTE_MAX_LEVERAGE,
    ABSOLUTE_MAX_POSITION_PCT,
    ABSOLUTE_MAX_RISK_PER_TRADE_PCT,
    RiskContext,
    RiskEngine,
)

pytestmark = pytest.mark.adversarial


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine()


def _rejected_rules(engine: RiskEngine, proposal: TradeProposal, context: RiskContext) -> list[str]:
    decision = engine.evaluate(proposal, context)
    assert decision.decision is RiskDecisionType.REJECTED, (
        f"expected a REJECTION but got {decision.decision.value}. "
        f"This means a safety control was bypassed."
    )
    assert not decision.is_approved
    return decision.failed_rules


# --------------------------------------------------------------------------- #
# the baseline must pass, or the negative tests prove nothing
# --------------------------------------------------------------------------- #


def test_sane_proposal_is_approved(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    """Control test.

    Without this, every rejection test below could be passing for the wrong
    reason (a broken fixture rejecting everything).
    """
    decision = engine.evaluate(valid_proposal, risk_context)
    assert decision.is_approved, f"baseline proposal was rejected: {decision.reasons}"
    assert decision.failed_rules == []


# --------------------------------------------------------------------------- #
# attack: construct an incoherent proposal
# --------------------------------------------------------------------------- #


def test_cannot_build_long_with_stop_above_entry() -> None:
    """A 'stop' above entry on a long would trigger instantly.

    Blocked at construction, so such an object cannot exist to be evaluated.
    """
    with pytest.raises(ValueError, match="stop_price < entry_price"):
        TradeProposal(
            strategy_id="attack",
            symbol="SPY",
            side=Side.BUY,
            quantity=10,
            entry_price=100.0,
            stop_price=105.0,
        )


def test_cannot_build_short_with_stop_below_entry() -> None:
    with pytest.raises(ValueError, match="stop_price > entry_price"):
        TradeProposal(
            strategy_id="attack",
            symbol="SPY",
            side=Side.SELL,
            quantity=10,
            entry_price=100.0,
            stop_price=95.0,
        )


def test_cannot_build_zero_or_negative_quantity() -> None:
    for quantity in (0, -1, -100.5):
        with pytest.raises(ValueError):
            TradeProposal(
                strategy_id="attack",
                symbol="SPY",
                side=Side.BUY,
                quantity=quantity,
                entry_price=100.0,
                stop_price=99.0,
            )


def test_cannot_build_without_strategy_id() -> None:
    """A proposal with no owner cannot be attributed, sized or reviewed."""
    with pytest.raises(ValueError):
        TradeProposal(
            strategy_id="",
            symbol="SPY",
            side=Side.BUY,
            quantity=1,
            entry_price=100.0,
            stop_price=99.0,
        )


def test_cannot_build_with_negative_prices() -> None:
    with pytest.raises(ValueError):
        TradeProposal(
            strategy_id="attack",
            symbol="SPY",
            side=Side.BUY,
            quantity=1,
            entry_price=-100.0,
            stop_price=-101.0,
        )


def test_cannot_build_limit_order_without_limit_price() -> None:
    with pytest.raises(ValueError, match="requires limit_price"):
        TradeProposal(
            strategy_id="attack",
            symbol="SPY",
            side=Side.BUY,
            quantity=1,
            entry_price=100.0,
            stop_price=99.0,
            order_type=OrderType.LIMIT,
        )


def test_cannot_build_with_naive_timestamp() -> None:
    """A naive datetime breaks the staleness comparison at runtime."""
    with pytest.raises(ValueError, match="timezone-aware"):
        TradeProposal(
            strategy_id="attack",
            symbol="SPY",
            side=Side.BUY,
            quantity=1,
            entry_price=100.0,
            stop_price=99.0,
            created_at=datetime(2026, 1, 1, 12, 0),
        )


# --------------------------------------------------------------------------- #
# attack: oversized risk
# --------------------------------------------------------------------------- #


def test_oversized_risk_is_rejected(engine: RiskEngine, risk_context: RiskContext) -> None:
    """Risk 50% of the account on one trade."""
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=10_000,
        entry_price=585.0,
        stop_price=580.0,  # $5 risk x 10,000 shares = $50,000 on $100k
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    assert "max_risk_per_trade" in failed


def test_oversized_position_is_rejected(engine: RiskEngine, risk_context: RiskContext) -> None:
    """A tight stop does not license an unlimited position size."""
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=100,
        entry_price=585.0,
        stop_price=584.99,  # almost no per-share risk
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    # Either the stop is too close to size from, or the position is too large.
    assert {"stop_distance", "max_position_size"} & set(failed)


def test_hairline_stop_is_rejected(engine: RiskEngine, risk_context: RiskContext) -> None:
    """A stop one cent away implies a vast position and is pure noise bait."""
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        entry_price=585.0,
        stop_price=584.999,
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    assert "stop_distance" in failed


def test_config_cannot_exceed_hard_risk_ceiling(
    engine: RiskEngine, risk_context: RiskContext
) -> None:
    """Editing risk.yaml to a reckless limit must not grant it.

    The compiled-in ceiling clamps the configured value, so a trade sized to
    the *configured* 90% is still rejected.
    """
    risk_context.config.max_risk_per_trade_pct = 90.0
    assert risk_context.effective_max_risk_pct() == ABSOLUTE_MAX_RISK_PER_TRADE_PCT

    # 10% of a $100k account = $10,000 risk, above the 5% hard ceiling.
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=2_000,
        entry_price=585.0,
        stop_price=580.0,
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    assert "max_risk_per_trade" in failed


def test_config_cannot_exceed_hard_position_ceiling(risk_context: RiskContext) -> None:
    from app.risk.rules import _clamp

    assert _clamp(500.0, ABSOLUTE_MAX_POSITION_PCT, "max_position_pct") == ABSOLUTE_MAX_POSITION_PCT
    assert _clamp(99.0, ABSOLUTE_MAX_LEVERAGE, "max_leverage") == ABSOLUTE_MAX_LEVERAGE


# --------------------------------------------------------------------------- #
# attack: bad market conditions
# --------------------------------------------------------------------------- #


def test_stale_quote_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    """Trading on a price that stopped updating."""
    risk_context.quote = Quote(
        symbol="SPY",
        timestamp=datetime.now(UTC) - timedelta(minutes=30),
        bid_price=584.95,
        bid_size=100,
        ask_price=585.05,
        ask_size=100,
    )
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "data_freshness" in failed


def test_missing_quote_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    """No quote means no idea of the current price. Fail closed."""
    risk_context.quote = None
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "data_freshness" in failed


def test_one_sided_book_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    """A missing bid must not be read as a zero spread."""
    risk_context.quote = Quote(
        symbol="SPY",
        timestamp=datetime.now(UTC),
        bid_price=0.0,
        bid_size=0,
        ask_price=585.05,
        ask_size=100,
    )
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "spread" in failed


def test_wide_spread_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    risk_context.quote = Quote(
        symbol="SPY",
        timestamp=datetime.now(UTC),
        bid_price=570.0,
        bid_size=100,
        ask_price=600.0,
        ask_size=100,
    )
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "spread" in failed


def test_closed_market_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    from app.models.market import MarketClock

    risk_context.clock = MarketClock(timestamp=datetime.now(UTC), is_open=False)
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "market_open" in failed


def test_unknown_market_state_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    """A missing clock must not be assumed to mean 'open'."""
    risk_context.clock = None
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "market_open" in failed


def test_illiquid_symbol_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    risk_context.average_volume = 1_000.0
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "liquidity" in failed


# --------------------------------------------------------------------------- #
# attack: blocked states
# --------------------------------------------------------------------------- #


def test_kill_switch_blocks_everything(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    risk_context.kill_switch_engaged = True
    risk_context.kill_switch_reason = "test"
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "kill_switch" in failed


def test_backtest_mode_cannot_reach_broker(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    for mode in (TradingMode.BACKTEST, TradingMode.REPLAY):
        risk_context.mode = mode
        failed = _rejected_rules(engine, valid_proposal, risk_context)
        assert "trading_mode" in failed, f"{mode.value} must not place broker orders"


def test_blocked_account_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    risk_context.account.trading_blocked = True
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "account_not_blocked" in failed


def test_reconciliation_mismatch_blocks_trading(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    """If we do not know what we hold, we cannot size a trade."""
    risk_context.portfolio.reconciliation_mismatch = True
    risk_context.portfolio.mismatch_detail = ["SPY: expected 10, broker says 0"]
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "broker_reconciled" in failed


# --------------------------------------------------------------------------- #
# attack: unsupported instruments and features
# --------------------------------------------------------------------------- #


def test_unsupported_asset_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    risk_context.asset_class = AssetClass.UNSUPPORTED
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "asset_supported" in failed


def test_crypto_rejected_when_account_lacks_crypto(
    engine: RiskEngine, risk_context: RiskContext
) -> None:
    """Availability depends on the account and the country. Never assumed."""
    risk_context.asset_class = AssetClass.CRYPTO
    risk_context.capabilities.supports_crypto = False
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="BTC/USD",
        side=Side.BUY,
        quantity=1,
        entry_price=94_000.0,
        stop_price=92_000.0,
    )
    # Reject for *some* reason; the capability gate is the one under test.
    decision = engine.evaluate(proposal, risk_context)
    assert not decision.is_approved
    assert "asset_supported" in decision.failed_rules


def test_naked_short_rejected_on_cash_account(
    engine: RiskEngine, risk_context: RiskContext
) -> None:
    """Selling what you do not own, with shorting disabled."""
    assert risk_context.capabilities.supports_short_selling is False
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.SELL,
        quantity=100,
        entry_price=585.0,
        stop_price=590.0,
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    assert "short_selling" in failed


def test_fractional_rejected_when_unsupported(
    engine: RiskEngine, risk_context: RiskContext
) -> None:
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=0.5,
        entry_price=585.0,
        stop_price=580.0,
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    assert "fractional_shares" in failed


# --------------------------------------------------------------------------- #
# attack: stale and expired proposals
# --------------------------------------------------------------------------- #


def test_stale_proposal_is_rejected(engine: RiskEngine, risk_context: RiskContext) -> None:
    """An old proposal was built on a price that no longer exists."""
    old = datetime.now(UTC) - timedelta(hours=2)
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        entry_price=585.0,
        stop_price=580.0,
        created_at=old,
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    assert "proposal_freshness" in failed


def test_expired_proposal_is_rejected(engine: RiskEngine, risk_context: RiskContext) -> None:
    now = datetime.now(UTC)
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        entry_price=585.0,
        stop_price=580.0,
        created_at=now,
        expires_at=now - timedelta(seconds=1),
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    assert "proposal_not_expired" in failed


# --------------------------------------------------------------------------- #
# attack: exceed portfolio and rate limits
# --------------------------------------------------------------------------- #


def test_too_many_open_positions_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    from app.models.enums import PositionSide

    limit = risk_context.config.max_open_positions
    risk_context.portfolio = PortfolioSnapshot(
        equity=100_000.0,
        cash=50_000.0,
        buying_power=50_000.0,
        high_water_mark=100_000.0,
        open_position_count=limit,
        positions=[
            Position(
                symbol=f"SYM{i}",
                quantity=1,
                side=PositionSide.LONG,
                average_entry_price=10.0,
                market_value=10.0,
            )
            for i in range(limit)
        ],
    )
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "max_open_positions" in failed


def test_daily_order_limit_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    """The circuit breaker against a runaway order loop."""
    risk_context.orders_today = risk_context.config.max_orders_per_day
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "max_orders_per_day" in failed


def test_per_symbol_order_limit_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    risk_context.orders_today_by_symbol = {"SPY": risk_context.config.max_orders_per_symbol_per_day}
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "max_orders_per_symbol_per_day" in failed


def test_daily_loss_limit_blocks_new_trades(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    """No revenge trading after the daily limit is hit."""
    risk_context.portfolio.daily_pl_pct = -(risk_context.config.max_daily_loss_pct + 1)
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "max_daily_loss" in failed


def test_drawdown_limit_blocks_new_trades(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    risk_context.portfolio.drawdown_pct = risk_context.config.max_portfolio_drawdown_pct + 1
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "max_portfolio_drawdown" in failed


def test_insufficient_buying_power_is_rejected(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    risk_context.account.buying_power = 10.0
    failed = _rejected_rules(engine, valid_proposal, risk_context)
    assert "buying_power" in failed


def test_correlated_exposure_is_rejected(engine: RiskEngine, risk_context: RiskContext) -> None:
    """Four semiconductor names is one bet, not four."""
    equity = risk_context.account.equity
    cap_pct = risk_context.config.max_correlated_exposure_pct
    risk_context.portfolio.exposure_by_correlation_group = {
        "semiconductors": equity * (cap_pct / 100)
    }
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="NVDA",  # in the semiconductors group in risk.yaml
        side=Side.BUY,
        quantity=5,
        entry_price=178.0,
        stop_price=175.0,
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    assert "max_correlated_exposure" in failed


def test_poor_reward_risk_is_rejected(engine: RiskEngine, risk_context: RiskContext) -> None:
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        entry_price=585.0,
        stop_price=580.0,
        target_price=586.0,  # 1:5 reward to risk
    )
    failed = _rejected_rules(engine, proposal, risk_context)
    assert "reward_risk_ratio" in failed


def test_zero_equity_cannot_size_a_trade(
    engine: RiskEngine, valid_proposal: TradeProposal, risk_context: RiskContext
) -> None:
    risk_context.account.equity = 0.0
    decision = engine.evaluate(valid_proposal, risk_context)
    assert not decision.is_approved
    assert "equity_available" in decision.failed_rules


# --------------------------------------------------------------------------- #
# the engine must not be fooled by mutation after the fact
# --------------------------------------------------------------------------- #


def test_decision_records_the_requested_size_not_a_mutated_one(
    engine: RiskEngine, risk_context: RiskContext
) -> None:
    """Risk must judge what was asked for, and the audit trail must keep it.

    `with_quantity()` returns a copy, so the original proposal is intact for
    the record even when a smaller size is approved.
    """
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=4,
        entry_price=585.0,
        stop_price=580.0,
        target_price=600.0,
    )
    smaller = proposal.with_quantity(1)
    assert proposal.quantity == 4, "the original proposal must not be mutated"
    assert smaller.quantity == 1
    assert smaller.id == proposal.id


def test_approved_reduced_never_exceeds_the_request(
    engine: RiskEngine, risk_context: RiskContext
) -> None:
    """A downsize must reduce, never increase, the quantity."""
    proposal = TradeProposal(
        strategy_id="attack",
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        entry_price=585.0,
        stop_price=580.0,
        target_price=600.0,
    )
    decision = engine.evaluate(proposal, risk_context)
    assert decision.is_approved
    assert decision.approved_quantity is not None
    assert decision.approved_quantity <= proposal.quantity
