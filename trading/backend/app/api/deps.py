"""FastAPI dependencies.

The runtime is attached to `app.state` at startup and read from the request
here. No module-level singleton, so tests can mount the same routers against a
runtime built with a simulated broker.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.agents.manager import AgentManager
from app.runtime import AtlasRuntime


def get_runtime(request: Request) -> AtlasRuntime:
    runtime: AtlasRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ATLAS runtime is not initialised yet",
        )
    return runtime


def get_agent_manager(
    runtime: Annotated[AtlasRuntime, Depends(get_runtime)],
) -> AgentManager:
    if runtime.agents is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agents are not initialised yet",
        )
    return runtime.agents


RuntimeDep = Annotated[AtlasRuntime, Depends(get_runtime)]
AgentManagerDep = Annotated[AgentManager, Depends(get_agent_manager)]
