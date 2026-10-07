"""Agent routes: list, inspect, start, stop, network graph."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.agents.manager import AgentManagerError
from app.api.deps import AgentManagerDep, RuntimeDep

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("")
async def list_agents(
    manager: AgentManagerDep,
    log_limit: int = Query(default=10, ge=0, le=200),
) -> dict[str, Any]:
    """Every agent with its status, task and recent logs."""
    return {
        "agents": [d.model_dump(mode="json") for d in manager.descriptors(log_limit=log_limit)],
        "health": manager.health_summary(),
    }


@router.get("/graph")
async def get_agent_graph(manager: AgentManagerDep) -> dict[str, Any]:
    """The agent network.

    Edges are derived from declared outputs and subscriptions, so the diagram
    always matches the real wiring rather than a hand-maintained picture.
    """
    return manager.graph().model_dump(mode="json")


@router.get("/{agent_id}")
async def get_agent(
    manager: AgentManagerDep,
    agent_id: str,
    log_limit: int = Query(default=50, ge=0, le=200),
) -> dict[str, Any]:
    """One agent in detail: what it is doing, why, its inputs and outputs."""
    agent = manager.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no agent '{agent_id}'")

    payload: dict[str, Any] = {
        "agent": agent.descriptor(log_limit=log_limit).model_dump(mode="json")
    }
    # Every agent has detail(); subclasses override it with domain
    # information (scanner rankings, risk counters, stream health).
    try:
        payload["detail"] = agent.detail()
    except Exception as exc:
        payload["detail"] = {"error": f"detail() failed: {exc}"}
    return payload


@router.post("/{agent_id}/start")
async def start_agent(manager: AgentManagerDep, agent_id: str) -> dict[str, Any]:
    try:
        agent = await manager.start_agent(agent_id)
    except AgentManagerError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"agent": agent.descriptor().model_dump(mode="json")}


@router.post("/{agent_id}/stop")
async def stop_agent(manager: AgentManagerDep, agent_id: str) -> dict[str, Any]:
    """Stop an agent.

    The Risk and Execution agents are protected and refuse: stopping the Risk
    Agent would remove the veto authority the Execution Agent depends on. Use
    the kill switch to halt trading instead.
    """
    try:
        agent = await manager.stop_agent(agent_id)
    except AgentManagerError as exc:
        message = str(exc)
        raise HTTPException(
            status_code=(
                status.HTTP_403_FORBIDDEN if "protected" in message else status.HTTP_404_NOT_FOUND
            ),
            detail=message,
        ) from exc
    return {"agent": agent.descriptor().model_dump(mode="json")}


@router.post("/{agent_id}/restart")
async def restart_agent(manager: AgentManagerDep, agent_id: str) -> dict[str, Any]:
    try:
        agent = await manager.restart_agent(agent_id)
    except AgentManagerError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"agent": agent.descriptor().model_dump(mode="json")}


@router.get("/briefing/summary")
async def get_briefing(runtime: RuntimeDep) -> dict[str, Any]:
    """The Orchestrator's aggregated briefing for the Command Center."""
    orchestrator = runtime.agents.get("orchestrator") if runtime.agents else None
    briefing = getattr(orchestrator, "briefing", None)
    if callable(briefing):
        return briefing()
    return {"detail": "orchestrator is not available"}
