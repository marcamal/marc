"""Journal, mentor explanations, and the decision-trace endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import RuntimeDep

router = APIRouter(tags=["journal"])


@router.get("/journal")
async def get_journal(
    runtime: RuntimeDep,
    limit: int = Query(default=50, ge=1, le=500),
    symbol: str | None = Query(default=None),
    search: str | None = Query(default=None, description="Free-text search of title and body"),
) -> dict[str, Any]:
    """The searchable trading journal."""
    entries = await runtime.queries.journal(limit=limit, symbol=symbol, search=search)
    return {"entries": entries, "count": len(entries)}


@router.get("/mentor/explanations")
async def get_explanations(
    runtime: RuntimeDep, limit: int = Query(default=20, ge=1, le=100)
) -> dict[str, Any]:
    """Recent teaching notes from the Mentor Agent."""
    agent = runtime.agents.get("mentor") if runtime.agents else None
    if agent is None:
        return {"explanations": []}

    explanations = getattr(agent, "explanations", [])
    return {
        "explanations": [
            {
                "title": e.title,
                "body": e.body,
                "symbol": e.symbol,
                "category": e.category,
                "lessons": e.lessons,
                "timestamp": e.timestamp.isoformat(),
            }
            for e in explanations[:limit]
        ]
    }


@router.get("/trace/{trace_id}")
async def get_trace(runtime: RuntimeDep, trace_id: str) -> dict[str, Any]:
    """Every record sharing one trace id — the full decision chain.

    This answers "WHY DID ATLAS BUY THIS?". One trace id threads through the
    signal, the proposal, the risk decision and the order, so the whole chain
    comes back as one story:

        scanner found NVDA -> technical scored it -> strategy proposed
        -> risk approved -> execution submitted -> broker filled
    """
    trace = await runtime.queries.decision_trace(trace_id)

    total = sum(len(v) for v in trace.values() if isinstance(v, list))
    if total == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no records for trace '{trace_id}'. Traces are created when a "
                f"strategy produces a proposal; check /api/system/events for "
                f"recent trace ids."
            ),
        )

    # Flatten into one chronological timeline, which is how a human reads it.
    timeline: list[dict[str, Any]] = []
    for stage, rows in trace.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            timeline.append({"stage": stage, **row})
    timeline.sort(key=lambda item: str(item.get("created_at") or ""))

    return {"trace_id": trace_id, "records": trace, "timeline": timeline, "count": total}


@router.get("/proposals")
async def get_proposals(
    runtime: RuntimeDep, limit: int = Query(default=50, ge=1, le=500)
) -> dict[str, Any]:
    """Recent trade proposals, approved and rejected alike.

    The rejected ones are the more informative half: they show when a strategy
    keeps generating trades the risk rules will never allow.
    """
    return {"proposals": await runtime.queries.recent_proposals(limit=limit)}
