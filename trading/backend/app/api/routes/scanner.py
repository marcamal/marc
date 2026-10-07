"""Scanner routes: ranked candidates and an on-demand scan."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import RuntimeDep

router = APIRouter(prefix="/scanner", tags=["scanner"])


def _scanner(runtime: RuntimeDep) -> Any:
    agent = runtime.agents.get("scanner") if runtime.agents else None
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scanner agent is not registered",
        )
    return agent


@router.get("/results")
async def get_results(
    runtime: RuntimeDep, limit: int = Query(default=20, ge=1, le=100)
) -> dict[str, Any]:
    """The latest ranked candidates.

    These are observations, not trade instructions. Nothing in ATLAS trades
    off a scanner result directly.
    """
    agent = _scanner(runtime)
    results = getattr(agent, "results", [])
    return {
        "universe": runtime.config.scanner.active,
        "universe_size": len(runtime.config.scanner.active_symbols),
        "results": [r.model_dump(mode="json") for r in results[:limit]],
        "excluded": getattr(agent, "excluded", {}),
        "agent_status": agent.status.value,
        "weights": runtime.config.scanner.ranking.weights,
        "disclaimer": (
            "Scores are relative to the symbols in this scan, not absolute. "
            "A top rank on a quiet day means 'least boring', not 'good setup'."
        ),
    }


@router.post("/run")
async def run_scan(runtime: RuntimeDep) -> dict[str, Any]:
    """Run one scan now, without waiting for the next cycle."""
    agent = _scanner(runtime)
    try:
        await agent.run_cycle()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"scan failed: {exc}"
        ) from exc

    results = getattr(agent, "results", [])
    return {
        "scanned": getattr(agent, "last_scanned_count", 0),
        "ranked": len(results),
        "results": [r.model_dump(mode="json") for r in results],
    }


@router.get("/universes")
async def get_universes(runtime: RuntimeDep) -> dict[str, Any]:
    """The configured universes.

    `streaming_limit` is the real constraint on the free Alpaca data plan:
    30 symbols on one websocket. Universes larger than that are still
    scannable over REST, but only the first 30 symbols stream live.
    """
    config = runtime.config.scanner
    return {
        "active": config.active,
        "universes": dict(config.universes),
        "sizes": {name: len(symbols) for name, symbols in config.universes.items()},
        "streaming_limit": runtime.settings.effective_max_stream_symbols,
        "note": (
            "The free Alpaca data plan allows 30 streaming symbols on one connection. "
            "Larger universes are still scanned over REST, but do not receive live "
            "websocket updates."
        ),
    }
