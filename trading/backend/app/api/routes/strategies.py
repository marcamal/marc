"""Strategy routes: list, enable, disable.

Enabling from here is **session-only** and never written back to
`strategies.yaml`. A restart returns to the configured state, so an
experiment cannot quietly become permanent. The global switch
(`strategies_globally_enabled`) can only be changed in the file — there is no
API for it, because that is the master brake.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.api.deps import RuntimeDep

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("")
async def list_strategies(runtime: RuntimeDep) -> dict[str, Any]:
    registry = runtime.strategies
    return {
        **registry.status(),
        "warning": (
            "No strategy here has been backtested. Enabling one means it can "
            "generate trade proposals, which the Risk Agent will then judge. "
            "Paper mode only."
        ),
    }


@router.get("/{strategy_id}")
async def get_strategy(runtime: RuntimeDep, strategy_id: str) -> dict[str, Any]:
    strategy = runtime.strategies.get(strategy_id)
    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no strategy '{strategy_id}'"
        )
    return {"strategy": strategy.stats()}


@router.post("/{strategy_id}/enable")
async def enable_strategy(runtime: RuntimeDep, strategy_id: str) -> dict[str, Any]:
    """Enable one strategy for this session.

    Refused in LIVE mode. Turning on an unproven strategy against real money
    from a dashboard button is exactly the mistake this system exists to
    prevent; `strategies.yaml` plus a restart is the deliberate path.
    """
    from app.config.settings import TradingMode

    strategy = runtime.strategies.get(strategy_id)
    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no strategy '{strategy_id}'"
        )

    if runtime.settings.effective_mode is TradingMode.LIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Strategies cannot be enabled from the API in LIVE mode. Edit "
                "strategies.yaml and restart ATLAS, so that enabling a strategy "
                "against real money is a deliberate act."
            ),
        )

    strategy.enable()

    notes: list[str] = []
    if not runtime.strategies.globally_enabled:
        notes.append(
            "strategies_globally_enabled is still false in strategies.yaml, so this "
            "strategy will NOT run. That master switch can only be changed in the file."
        )
    if runtime.kill_switch.is_engaged:
        notes.append(f"kill switch is engaged: {runtime.kill_switch.reason}")

    return {"strategy": strategy.stats(), "notes": notes, "persisted": False}


@router.post("/{strategy_id}/disable")
async def disable_strategy(runtime: RuntimeDep, strategy_id: str) -> dict[str, Any]:
    strategy = runtime.strategies.get(strategy_id)
    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no strategy '{strategy_id}'"
        )
    strategy.disable()
    return {"strategy": strategy.stats(), "persisted": False}
