"""Portfolio snapshot construction and broker reconciliation.

Pure functions, so the exposure and drawdown arithmetic is unit-testable
without a broker.

**Reconciliation rule: the broker always wins.** If ATLAS believes it holds
100 shares and Alpaca says 50, Alpaca is right — it is the system of record.
ATLAS adopts the broker's numbers and raises a loud mismatch flag rather than
trying to reconcile cleverly. A mismatch also blocks new orders (see the
`broker_reconciled` risk rule), because sizing decisions made against a wrong
position are worse than no decision at all.
"""

from __future__ import annotations

from app.config.schema import RiskConfig
from app.models.trading import AccountSnapshot, PortfolioSnapshot, Position


def reconcile_positions(
    broker_positions: list[Position],
    internal_positions: dict[str, float],
    tolerance: float = 1e-6,
) -> list[str]:
    """Compare ATLAS's expected positions with the broker's.

    Args:
        broker_positions: what the broker reports. Authoritative.
        internal_positions: symbol -> quantity ATLAS believes it holds.
        tolerance: fractional-share rounding allowance.

    Returns:
        Human-readable mismatch descriptions. Empty means agreement.
    """
    mismatches: list[str] = []
    broker_quantities = {p.symbol: p.quantity for p in broker_positions}

    for symbol, expected in internal_positions.items():
        actual = broker_quantities.get(symbol, 0.0)
        if abs(actual - expected) > tolerance:
            mismatches.append(f"{symbol}: ATLAS expected {expected:g}, broker reports {actual:g}")

    for symbol, actual in broker_quantities.items():
        if symbol not in internal_positions and abs(actual) > tolerance:
            # Not necessarily a bug: a manual trade in the Alpaca dashboard
            # looks exactly like this. Worth surfacing either way.
            mismatches.append(
                f"{symbol}: broker reports {actual:g} that ATLAS did not open "
                f"(opened manually, or by an earlier session)"
            )

    return mismatches


def build_snapshot(
    account: AccountSnapshot,
    positions: list[Position],
    config: RiskConfig,
    high_water_mark: float = 0.0,
    realized_pl_today: float = 0.0,
    internal_positions: dict[str, float] | None = None,
) -> PortfolioSnapshot:
    """Assemble a full portfolio view from broker state."""
    equity = account.equity
    total_exposure = sum(p.exposure for p in positions)

    # The high water mark only ever rises. Drawdown is measured from it, so
    # letting it fall would mask the drawdown it exists to reveal.
    watermark = max(high_water_mark, equity)
    drawdown_pct = ((watermark - equity) / watermark * 100) if watermark > 0 else 0.0

    exposure_by_group: dict[str, float] = {}
    for position in positions:
        group = config.correlation_group_for(position.symbol)
        if group:
            exposure_by_group[group] = exposure_by_group.get(group, 0.0) + position.exposure

    exposure_by_strategy: dict[str, float] = {}
    for position in positions:
        key = position.strategy_id or "unassigned"
        exposure_by_strategy[key] = exposure_by_strategy.get(key, 0.0) + position.exposure

    mismatches = (
        reconcile_positions(positions, internal_positions) if internal_positions is not None else []
    )

    return PortfolioSnapshot(
        equity=equity,
        cash=account.cash,
        buying_power=account.buying_power,
        positions=positions,
        total_exposure=round(total_exposure, 2),
        total_exposure_pct=round((total_exposure / equity * 100) if equity > 0 else 0.0, 4),
        unrealized_pl=round(sum(p.unrealized_pl for p in positions), 2),
        realized_pl_today=round(realized_pl_today, 2),
        daily_pl=round(account.daily_pl, 2),
        daily_pl_pct=round(account.daily_pl_pct, 4),
        high_water_mark=round(watermark, 2),
        drawdown_pct=round(drawdown_pct, 4),
        open_position_count=len(positions),
        exposure_by_correlation_group={k: round(v, 2) for k, v in exposure_by_group.items()},
        exposure_by_strategy={k: round(v, 2) for k, v in exposure_by_strategy.items()},
        reconciliation_mismatch=bool(mismatches),
        mismatch_detail=mismatches,
    )
