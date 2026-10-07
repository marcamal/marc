"""Risk routes: limits, decisions, events, and a read-only proposal check."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import RuntimeDep
from app.models.enums import OrderType, Side
from app.models.trading import TradeProposal

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/status")
async def get_risk_status(runtime: RuntimeDep) -> dict[str, Any]:
    """Current risk configuration, limits and counters."""
    agent = runtime.agents.get("risk") if runtime.agents else None
    detail = agent.detail() if agent is not None else {}

    from app.risk.rules import (
        ABSOLUTE_MAX_DAILY_LOSS_PCT,
        ABSOLUTE_MAX_LEVERAGE,
        ABSOLUTE_MAX_ORDERS_PER_DAY,
        ABSOLUTE_MAX_POSITION_PCT,
        ABSOLUTE_MAX_RISK_PER_TRADE_PCT,
        ABSOLUTE_MAX_TOTAL_EXPOSURE_PCT,
    )

    return {
        "config": runtime.config.risk.model_dump(),
        "agent": detail,
        "kill_switch": runtime.kill_switch.status(),
        "orders": runtime.order_tracker.status(),
        "portfolio": (
            runtime.portfolio_snapshot.model_dump(mode="json")
            if runtime.portfolio_snapshot
            else None
        ),
        # Compiled-in ceilings. risk.yaml can only ever be stricter than these;
        # a config value above a ceiling is clamped, loudly, at runtime.
        "hard_ceilings": {
            "max_risk_per_trade_pct": ABSOLUTE_MAX_RISK_PER_TRADE_PCT,
            "max_position_pct": ABSOLUTE_MAX_POSITION_PCT,
            "max_total_exposure_pct": ABSOLUTE_MAX_TOTAL_EXPOSURE_PCT,
            "max_leverage": ABSOLUTE_MAX_LEVERAGE,
            "max_daily_loss_pct": ABSOLUTE_MAX_DAILY_LOSS_PCT,
            "max_orders_per_day": ABSOLUTE_MAX_ORDERS_PER_DAY,
        },
    }


@router.get("/decisions")
async def get_decisions(
    runtime: RuntimeDep,
    limit: int = Query(default=50, ge=1, le=500),
    stored: bool = Query(default=False, description="Read from the database instead of memory"),
) -> dict[str, Any]:
    """Recent risk decisions, from memory or from the database."""
    if stored:
        return {"decisions": await runtime.queries.recent_risk_decisions(limit=limit)}

    agent = runtime.agents.get("risk") if runtime.agents else None
    decisions = getattr(agent, "recent_decisions", []) if agent else []
    return {
        "decisions": [d.model_dump(mode="json") for d in decisions[:limit]],
        "source": "memory",
    }


@router.get("/events")
async def get_risk_events(
    runtime: RuntimeDep, limit: int = Query(default=50, ge=1, le=500)
) -> dict[str, Any]:
    return {"events": await runtime.queries.recent_risk_events(limit=limit)}


class ProposalCheckRequest(BaseModel):
    """A hypothetical trade to run past the Risk Agent."""

    symbol: str = Field(min_length=1, examples=["SPY"])
    side: Side = Side.BUY
    quantity: float = Field(gt=0, examples=[10])
    entry_price: float = Field(gt=0, examples=[585.0])
    stop_price: float = Field(gt=0, examples=[580.0])
    target_price: float | None = Field(default=None, examples=[595.0])
    strategy_id: str = Field(default="manual_check", min_length=1)


@router.post("/check")
async def check_proposal(runtime: RuntimeDep, request: ProposalCheckRequest) -> dict[str, Any]:
    """Evaluate a hypothetical trade. **Read-only — places no order.**

    Useful for learning what the limits actually permit at the current account
    size, and for seeing which rule would block an idea before trying it.
    Running this cannot produce an order: it calls the engine directly and
    never publishes a proposal onto the bus.
    """
    agent = runtime.agents.get("risk") if runtime.agents else None
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="risk agent is not running",
        )

    try:
        proposal = TradeProposal(
            strategy_id=request.strategy_id,
            symbol=request.symbol.upper(),
            side=request.side,
            quantity=request.quantity,
            entry_price=request.entry_price,
            stop_price=request.stop_price,
            target_price=request.target_price,
            order_type=OrderType.MARKET,
        )
    except ValueError as exc:
        # The proposal model's own validators rejected it — a stop on the
        # wrong side of entry, for instance. That is itself the answer.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"the proposal is not internally coherent: {exc}",
        ) from exc

    # check() evaluates without publishing, so this endpoint cannot
    # accidentally start the execution pipeline. See RiskAgent.check().
    decision = await agent.check(proposal)

    return {
        "proposal": proposal.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "summary": decision.summary(),
        "note": "This was a simulation. No order was created or submitted.",
    }
