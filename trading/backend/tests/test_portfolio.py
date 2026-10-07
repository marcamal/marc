"""Portfolio snapshot and broker-reconciliation tests.

The reconciliation rule under test: **the broker always wins**, and a
disagreement must be surfaced rather than quietly resolved.
"""

from __future__ import annotations

import pytest

from app.config.schema import AtlasConfig
from app.models.enums import PositionSide
from app.models.trading import AccountSnapshot, Position
from app.portfolio.snapshot import build_snapshot, reconcile_positions
from app.runtime import AtlasRuntime


def _position(symbol: str, quantity: float, price: float = 100.0) -> Position:
    return Position(
        symbol=symbol,
        quantity=quantity,
        side=PositionSide.LONG,
        average_entry_price=price,
        current_price=price,
        market_value=quantity * price,
        cost_basis=quantity * price,
    )


# --------------------------------------------------------------------------- #
# reconciliation
# --------------------------------------------------------------------------- #


def test_matching_positions_reconcile_cleanly() -> None:
    broker = [_position("SPY", 10), _position("NVDA", 5)]
    internal = {"SPY": 10.0, "NVDA": 5.0}

    assert reconcile_positions(broker, internal) == []


def test_quantity_mismatch_is_reported() -> None:
    broker = [_position("SPY", 5)]
    internal = {"SPY": 10.0}

    mismatches = reconcile_positions(broker, internal)

    assert len(mismatches) == 1
    assert "SPY" in mismatches[0]
    assert "expected 10" in mismatches[0]
    assert "broker reports 5" in mismatches[0]


def test_position_missing_at_broker_is_reported() -> None:
    """ATLAS thinks it holds something the broker does not have."""
    mismatches = reconcile_positions([], {"SPY": 10.0})

    assert len(mismatches) == 1
    assert "broker reports 0" in mismatches[0]


def test_unexpected_broker_position_is_reported() -> None:
    """A manual trade in the Alpaca dashboard looks exactly like this."""
    mismatches = reconcile_positions([_position("TSLA", 3)], {})

    assert len(mismatches) == 1
    assert "TSLA" in mismatches[0]
    assert "did not open" in mismatches[0]


def test_fractional_rounding_is_tolerated() -> None:
    """Floating point noise must not be reported as a real mismatch."""
    assert reconcile_positions([_position("SPY", 10.0000000001)], {"SPY": 10.0}) == []


# --------------------------------------------------------------------------- #
# snapshot arithmetic
# --------------------------------------------------------------------------- #


def test_snapshot_computes_exposure(account: AccountSnapshot, config: AtlasConfig) -> None:
    positions = [_position("SPY", 10, 100.0), _position("NVDA", 5, 200.0)]

    snapshot = build_snapshot(account, positions, config.risk)

    assert snapshot.total_exposure == pytest.approx(2000.0)
    assert snapshot.total_exposure_pct == pytest.approx(2.0)  # 2000 / 100,000
    assert snapshot.open_position_count == 2


def test_snapshot_groups_correlated_exposure(account: AccountSnapshot, config: AtlasConfig) -> None:
    """Four semiconductor names must count as one concentrated bet."""
    positions = [
        _position("NVDA", 10, 100.0),
        _position("AMD", 10, 100.0),
        _position("SPY", 10, 100.0),
    ]

    snapshot = build_snapshot(account, positions, config.risk)

    assert snapshot.exposure_by_correlation_group["semiconductors"] == pytest.approx(2000.0)
    assert snapshot.exposure_by_correlation_group["us_broad_index"] == pytest.approx(1000.0)


def test_high_water_mark_only_rises(account: AccountSnapshot, config: AtlasConfig) -> None:
    """Letting the watermark fall would hide the drawdown it exists to show."""
    account.equity = 90_000.0

    snapshot = build_snapshot(account, [], config.risk, high_water_mark=100_000.0)

    assert snapshot.high_water_mark == 100_000.0
    assert snapshot.drawdown_pct == pytest.approx(10.0)


def test_new_high_raises_the_watermark(account: AccountSnapshot, config: AtlasConfig) -> None:
    account.equity = 120_000.0

    snapshot = build_snapshot(account, [], config.risk, high_water_mark=100_000.0)

    assert snapshot.high_water_mark == 120_000.0
    assert snapshot.drawdown_pct == pytest.approx(0.0)


def test_daily_pl_from_last_equity(config: AtlasConfig) -> None:
    account = AccountSnapshot(
        account_id="T",
        equity=98_000.0,
        last_equity=100_000.0,
        cash=98_000.0,
        buying_power=98_000.0,
    )

    snapshot = build_snapshot(account, [], config.risk)

    assert snapshot.daily_pl == pytest.approx(-2000.0)
    assert snapshot.daily_pl_pct == pytest.approx(-2.0)


def test_zero_equity_does_not_divide_by_zero(config: AtlasConfig) -> None:
    account = AccountSnapshot(
        account_id="T", equity=0.0, last_equity=0.0, cash=0.0, buying_power=0.0
    )

    snapshot = build_snapshot(account, [], config.risk)

    assert snapshot.total_exposure_pct == 0.0
    assert snapshot.daily_pl_pct == 0.0
    assert snapshot.drawdown_pct == 0.0


def test_snapshot_flags_a_mismatch(account: AccountSnapshot, config: AtlasConfig) -> None:
    snapshot = build_snapshot(
        account, [_position("SPY", 5)], config.risk, internal_positions={"SPY": 10.0}
    )

    assert snapshot.reconciliation_mismatch is True
    assert snapshot.mismatch_detail


def test_snapshot_without_internal_view_does_not_flag(
    account: AccountSnapshot, config: AtlasConfig
) -> None:
    """Passing no internal view means 'do not check', not 'everything differs'."""
    snapshot = build_snapshot(account, [_position("SPY", 5)], config.risk)

    assert snapshot.reconciliation_mismatch is False


# --------------------------------------------------------------------------- #
# the Portfolio Agent
# --------------------------------------------------------------------------- #


async def test_portfolio_agent_populates_on_start(runtime: AtlasRuntime) -> None:
    """The dashboard must not be blank for the first cycle interval."""
    agent = runtime.agents.get("portfolio")
    assert agent is not None
    assert agent.snapshot is not None  # type: ignore[attr-defined]
    assert runtime.portfolio_snapshot is not None
    assert runtime.portfolio_snapshot.equity == 100_000.0


async def test_portfolio_agent_seeds_from_broker_without_false_mismatch(
    runtime: AtlasRuntime,
) -> None:
    """Pre-existing positions must not be reported as a divergence."""
    from app.brokers.base import OrderRequest

    await runtime.broker.submit_order(
        OrderRequest(symbol="SPY", side="buy", quantity=1, client_order_id="pre-1")
    )

    agent = runtime.agents.get("portfolio")
    agent.internal_positions = {}  # type: ignore[attr-defined]
    await agent.run_cycle()  # type: ignore[union-attr]

    assert runtime.portfolio_snapshot is not None
    assert runtime.portfolio_snapshot.reconciliation_mismatch is False


async def test_portfolio_agent_tracks_fills(runtime: AtlasRuntime) -> None:
    """The internal view is built from ATLAS's own fills."""
    import asyncio
    from datetime import UTC, datetime

    from app.events.types import OrderUpdateEvent
    from app.models.enums import OrderStatus, OrderType, Side, TimeInForce
    from app.models.trading import Order

    agent = runtime.agents.get("portfolio")
    agent.internal_positions = {}  # type: ignore[attr-defined]

    await runtime.bus.publish(
        OrderUpdateEvent(
            order=Order(
                id="o1",
                client_order_id="c1",
                symbol="QQQ",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.FILLED,
                quantity=7,
                filled_quantity=7,
                average_fill_price=500.0,
                filled_at=datetime.now(UTC),
            ),
            event_type="fill",
        )
    )
    await asyncio.sleep(0.2)

    assert agent.internal_positions.get("QQQ") == 7  # type: ignore[attr-defined]


async def test_portfolio_agent_engages_kill_switch_on_daily_loss(
    runtime: AtlasRuntime,
) -> None:
    """A breached daily loss limit must stop trading automatically."""
    from app.models.trading import PortfolioSnapshot

    agent = runtime.agents.get("portfolio")
    limit = runtime.config.risk.max_daily_loss_pct

    await agent._check_risk_thresholds(  # type: ignore[union-attr]
        PortfolioSnapshot(equity=90_000.0, daily_pl_pct=-(limit + 0.5))
    )

    assert runtime.kill_switch.is_engaged is True
    assert "Daily loss limit breached" in (runtime.kill_switch.reason or "")


async def test_portfolio_agent_engages_kill_switch_on_drawdown(
    runtime: AtlasRuntime,
) -> None:
    from app.models.trading import PortfolioSnapshot

    agent = runtime.agents.get("portfolio")
    limit = runtime.config.risk.max_portfolio_drawdown_pct

    await agent._check_risk_thresholds(  # type: ignore[union-attr]
        PortfolioSnapshot(equity=80_000.0, drawdown_pct=limit + 1)
    )

    assert runtime.kill_switch.is_engaged is True
    assert "Drawdown limit breached" in (runtime.kill_switch.reason or "")


async def test_portfolio_agent_adopts_broker_state_after_a_mismatch(
    runtime: AtlasRuntime,
) -> None:
    """Broker wins: the internal view is corrected to match."""
    agent = runtime.agents.get("portfolio")
    agent.internal_positions = {"SPY": 999.0}  # type: ignore[attr-defined]

    await agent.run_cycle()  # type: ignore[union-attr]

    assert runtime.portfolio_snapshot is not None
    assert runtime.portfolio_snapshot.reconciliation_mismatch is True
    # ...and the next cycle starts from the broker's truth.
    assert agent.internal_positions == {}  # type: ignore[attr-defined]
