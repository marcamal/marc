"""Account, positions, orders and portfolio routes.

All of these read through the backend. **Alpaca credentials never reach the
browser** — the frontend talks to ATLAS, ATLAS talks to Alpaca. That is the
whole point of the architecture:

    Browser -> ATLAS backend -> Alpaca      (correct)
    Browser -> Alpaca                        (never)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import RuntimeDep
from app.brokers.base import BrokerError

router = APIRouter(tags=["account"])


def _broker_error(exc: BrokerError) -> HTTPException:
    """Translate a broker failure into a useful HTTP error.

    Credential problems get 502 with a specific hint, because "wrong key
    environment" is by far the most common cause and a generic 500 sends
    people hunting in the wrong place.
    """
    from app.brokers.base import BrokerAuthError, BrokerConnectionError

    if isinstance(exc, BrokerAuthError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Alpaca rejected the credentials. Check that ALPACA_API_KEY and "
                f"ALPACA_SECRET_KEY in .env are PAPER keys, generated while the "
                f"Alpaca dashboard was switched to Paper. Details: {exc}"
            ),
        )
    if isinstance(exc, BrokerConnectionError):
        return HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Could not reach Alpaca: {exc}",
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get("/account")
async def get_account(runtime: RuntimeDep) -> dict[str, Any]:
    """Equity, cash, buying power and account flags."""
    try:
        account = await runtime.broker.get_account()
    except BrokerError as exc:
        raise _broker_error(exc) from exc

    return {
        "account": account.model_dump(mode="json"),
        "mode": runtime.settings.effective_mode.value,
        "simulated": runtime.broker.name == "simulated",
    }


@router.get("/positions")
async def get_positions(runtime: RuntimeDep) -> dict[str, Any]:
    try:
        positions = await runtime.broker.get_positions()
    except BrokerError as exc:
        raise _broker_error(exc) from exc

    return {
        "positions": [p.model_dump(mode="json") for p in positions],
        "count": len(positions),
        "total_exposure": round(sum(p.exposure for p in positions), 2),
    }


@router.get("/orders")
async def get_orders(
    runtime: RuntimeDep,
    order_status: str = Query(default="all", alias="status", pattern="^(open|closed|all)$"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Orders from the broker, with ATLAS's own tracked view alongside."""
    try:
        orders = await runtime.broker.get_orders(status=order_status, limit=limit)
    except BrokerError as exc:
        raise _broker_error(exc) from exc

    return {
        "orders": [o.model_dump(mode="json") for o in orders],
        "count": len(orders),
        "tracker": runtime.order_tracker.status(),
    }


@router.get("/portfolio")
async def get_portfolio(runtime: RuntimeDep) -> dict[str, Any]:
    """The Portfolio Agent's reconciled snapshot.

    Falls back to a direct broker read when the agent has not produced one
    yet, so the dashboard is never blank at startup.
    """
    snapshot = runtime.portfolio_snapshot
    if snapshot is not None:
        return {"portfolio": snapshot.model_dump(mode="json"), "source": "portfolio_agent"}

    try:
        account = await runtime.broker.get_account()
        positions = await runtime.broker.get_positions()
    except BrokerError as exc:
        raise _broker_error(exc) from exc

    from app.portfolio.snapshot import build_snapshot

    built = build_snapshot(account=account, positions=positions, config=runtime.config.risk)
    return {"portfolio": built.model_dump(mode="json"), "source": "direct_broker_read"}


@router.get("/portfolio/equity-curve")
async def get_equity_curve(
    runtime: RuntimeDep, limit: int = Query(default=500, ge=1, le=5000)
) -> dict[str, Any]:
    """Stored equity snapshots, oldest first, for charting."""
    return {"points": await runtime.queries.equity_curve(limit=limit)}


@router.get("/orders/history")
async def get_order_history(
    runtime: RuntimeDep, limit: int = Query(default=100, ge=1, le=1000)
) -> dict[str, Any]:
    """Orders ATLAS recorded, including ones from previous sessions."""
    return {"orders": await runtime.queries.recent_orders(limit=limit)}
