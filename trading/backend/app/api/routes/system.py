"""System routes: status, health, logs, events, capabilities, kill switch."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, status

from app.api.deps import RuntimeDep
from app.config.settings import TradingMode
from app.core.logging import log_buffer

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
async def get_status(runtime: RuntimeDep) -> dict[str, Any]:
    """Everything the System page needs in one call."""
    return runtime.status()


@router.get("/health")
async def get_health(runtime: RuntimeDep) -> dict[str, Any]:
    """Liveness and readiness.

    Returns 200 even when components are degraded: the dashboard needs to be
    able to *display* that something is wrong, which it cannot do if this
    endpoint fails. Degradation is reported in the body.
    """
    broker_ok = await runtime.broker.ping()
    database_ok = await runtime.database.health_check()
    agents = runtime.agents.health_summary() if runtime.agents else {}

    components = {
        "broker": broker_ok,
        "database": database_ok,
        "event_bus": runtime.bus.stats()["running"],
        "agents": agents.get("all_healthy", False),
    }

    return {
        "status": "ok" if all(components.values()) else "degraded",
        "components": components,
        "kill_switch_engaged": runtime.kill_switch.is_engaged,
        "mode": runtime.settings.effective_mode.value,
        "agents": agents,
    }


@router.get("/capabilities")
async def get_capabilities(runtime: RuntimeDep) -> dict[str, Any]:
    """What this broker and account can actually do.

    Detected at runtime rather than assumed, because the product set depends
    on account type and country of residence. The `unsupported` list is what
    the UI renders as "UNSUPPORTED BY CURRENT BROKER".
    """
    capabilities = await runtime.get_capabilities()
    return {
        "capabilities": capabilities.model_dump(),
        "unsupported": capabilities.unsupported_summary(),
        "notes": capabilities.notes,
    }


@router.get("/logs")
async def get_logs(
    limit: int = Query(default=100, ge=1, le=1000),
    level: str | None = Query(default=None, description="Minimum level: DEBUG/INFO/WARNING/ERROR"),
) -> dict[str, Any]:
    """Recent log records from the in-memory ring buffer."""
    return {"logs": log_buffer.tail(limit=limit, min_level=level)}


@router.get("/events")
async def get_events(
    runtime: RuntimeDep,
    limit: int = Query(default=100, ge=1, le=500),
    pattern: str = Query(default="*", description="Topic filter, e.g. 'risk.*'"),
) -> dict[str, Any]:
    """Recent events from the bus history."""
    events = runtime.bus.history(limit=limit, pattern=pattern)
    return {
        "events": [e.model_dump(mode="json") for e in events],
        "bus": runtime.bus.stats(),
    }


# --------------------------------------------------------------------------- #
# kill switch
# --------------------------------------------------------------------------- #


@router.get("/kill-switch")
async def get_kill_switch(runtime: RuntimeDep) -> dict[str, Any]:
    return runtime.kill_switch.status()


@router.post("/kill-switch/engage")
async def engage_kill_switch(
    runtime: RuntimeDep,
    reason: str = Body(default="engaged from dashboard", embed=True),
) -> dict[str, Any]:
    """EMERGENCY STOP. Blocks all new orders immediately.

    Also cancels resting orders, and flattens positions only if
    `risk.yaml -> kill_switch.flatten_positions` is explicitly true.
    """
    event = await runtime.engage_kill_switch(reason=reason, triggered_by="dashboard")
    return {
        "engaged": True,
        "reason": event.reason,
        "orders_canceled": event.orders_canceled,
        "positions_flattened": event.positions_flattened,
        "status": runtime.kill_switch.status(),
    }


@router.post("/kill-switch/release")
async def release_kill_switch(runtime: RuntimeDep) -> dict[str, Any]:
    """Resume trading. Deletes the kill switch file as well as the flag."""
    await runtime.release_kill_switch(released_by="dashboard")
    return {"engaged": False, "status": runtime.kill_switch.status()}


# --------------------------------------------------------------------------- #
# mode
# --------------------------------------------------------------------------- #


@router.get("/mode")
async def get_mode(runtime: RuntimeDep) -> dict[str, Any]:
    """The trading mode and the state of each live-trading gate."""
    settings = runtime.settings
    return {
        "effective": settings.effective_mode.value,
        "requested": settings.requested_mode.value,
        "is_live": settings.effective_mode is TradingMode.LIVE,
        "is_simulated": runtime.broker.name == "simulated",
        "live_gates": settings.live_gates,
        "warnings": settings.mode_warnings(),
        "explanation": (
            "Live trading requires three independent conditions, all set in .env: "
            "ATLAS_TRADING_MODE=live, ATLAS_LIVE_TRADING_ENABLED=true, and "
            "ATLAS_MANUAL_LIVE_CONFIRMATION=I_UNDERSTAND_THE_RISK. "
            "There is deliberately no way to switch to live mode from this API."
        ),
    }


@router.post("/maintenance/purge")
async def purge_old_data(runtime: RuntimeDep) -> dict[str, Any]:
    """Apply the data retention policy now."""
    try:
        deleted = await runtime.database.purge_old_rows()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"purge failed: {exc}",
        ) from exc
    return {"deleted": deleted}
