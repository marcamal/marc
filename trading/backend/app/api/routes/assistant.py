"""Assistant routes: ask a question, stream an answer, read the AI's status.

Note what is *not* here. There is no endpoint that lets the assistant act -
no `/assistant/trade`, no `/assistant/execute`, no tool-calling bridge. The
assistant's entire API surface is "send text, get text", which is the whole
reason it is safe to let it read the account.

The streaming endpoint uses server-sent events rather than a websocket: the
dashboard already has one websocket for the event bus, and SSE is a plain
HTTP response that needs no extra client machinery.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents.assistant_agent import SUGGESTED_QUESTIONS, AssistantAgent
from app.api.deps import RuntimeDep
from app.core.logging import get_logger
from app.runtime import AtlasRuntime

log = get_logger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"])


def _assistant(runtime: AtlasRuntime) -> AssistantAgent:
    """The running Assistant Agent, or a 503 that says how to start it."""
    manager = runtime.agents
    if manager is not None:
        for agent in manager.by_type("assistant"):
            if isinstance(agent, AssistantAgent):
                return agent
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "The Assistant Agent is not registered. Add an entry with "
            "'type: assistant' to config/agents.yaml and restart."
        ),
    )


class AskRequest(BaseModel):
    """One question for the assistant."""

    question: str = Field(
        min_length=1,
        max_length=4000,
        examples=["What is in my account, and what is it worth?"],
    )
    #: Lets the dashboard keep separate threads (chat, briefing) without
    #: them bleeding into each other.
    conversation_id: str = Field(default="default", min_length=1, max_length=64)
    #: Restrict the context to named sections, which makes an answer cheaper
    #: and more focused. Omit for everything.
    sections: list[str] | None = Field(
        default=None,
        examples=[["account", "positions"]],
        description="Context sections to include, e.g. account, positions, risk, scanner.",
    )


@router.post("/ask")
async def ask(runtime: RuntimeDep, request: AskRequest) -> dict[str, Any]:
    """Answer a question about the account, the system or the market.

    Read-only. The answer is text; nothing in ATLAS acts on it.
    """
    agent = _assistant(runtime)
    sections = {s.strip().lower() for s in request.sections} if request.sections else None

    try:
        answer = await agent.ask(
            request.question,
            conversation_id=request.conversation_id,
            sections=sections,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    payload = answer.as_dict()
    payload["note"] = (
        "The assistant reads ATLAS state and explains it. It cannot place an "
        "order, change a risk limit, enable a strategy or release the kill "
        "switch. Not financial advice."
    )
    return payload


@router.post("/stream")
async def ask_streaming(runtime: RuntimeDep, request: AskRequest) -> StreamingResponse:
    """The same question, answered as server-sent events.

    Event names: `chunk` (a piece of text), `error` (a reason, then the stream
    ends), `done` (the session cost so far).
    """
    agent = _assistant(runtime)

    async def events() -> AsyncIterator[str]:
        try:
            async for kind, payload in agent.ask_stream(
                request.question, conversation_id=request.conversation_id
            ):
                yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"
        except ValueError as exc:
            yield f"event: error\ndata: {json.dumps(str(exc))}\n\n"
        except Exception as exc:  # pragma: no cover - defensive
            # An exception inside a streaming response cannot become a 500,
            # because the headers are already sent. Deliver it as an event so
            # the chat shows the reason instead of just stopping.
            log.exception("assistant stream crashed")
            yield f"event: error\ndata: {json.dumps(f'{type(exc).__name__}: {exc}')}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Without this, a reverse proxy may buffer the whole response and
            # defeat the point of streaming.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status")
async def assistant_status(runtime: RuntimeDep) -> dict[str, Any]:
    """What the assistant is, what it costs, and what it cannot do."""
    agent = _assistant(runtime)
    detail = agent.detail()
    return {
        "agent_status": agent.status.value,
        **detail,
        "setup": {
            "configured": agent.provider.name != "deterministic",
            "env_var": "ANTHROPIC_API_KEY",
            "how_to_enable": (
                "Put ANTHROPIC_API_KEY=sk-ant-... in trading/.env and restart the "
                "backend. Without it the assistant answers from ATLAS's own "
                "figures; everything else works either way."
            ),
        },
    }


@router.get("/suggestions")
async def suggestions() -> dict[str, Any]:
    """Starter questions for an empty chat. No runtime needed."""
    return {"questions": list(SUGGESTED_QUESTIONS)}


@router.get("/context")
async def preview_context(
    runtime: RuntimeDep,
    sections: str | None = Query(
        default=None, description="Comma-separated section names, or omit for all"
    ),
) -> dict[str, Any]:
    """Exactly what the model would be shown.

    Exposed because "what does the AI know about me?" deserves a literal
    answer rather than a promise. If a credential ever appeared in a context,
    this endpoint is where it would be caught.
    """
    agent = _assistant(runtime)
    wanted = {s.strip().lower() for s in sections.split(",") if s.strip()} if sections else None
    context = agent.context_builder.build(include=wanted)
    return {
        "context": context,
        "characters": len(context),
        "sections": [line[3:].strip() for line in context.splitlines() if line.startswith("## ")],
        "note": (
            "This is the complete snapshot sent to the model. It contains no API "
            "keys, no file paths and no database URL, and it is built from "
            "in-memory state without calling the broker."
        ),
    }


@router.post("/reset")
async def reset(
    runtime: RuntimeDep,
    conversation_id: str = Query(default="default"),
    budget: bool = Query(default=False, description="Also reset the session cost counter"),
) -> dict[str, Any]:
    """Forget a conversation, and optionally the spend counter."""
    agent = _assistant(runtime)
    agent.reset(conversation_id)
    if budget:
        agent.reset_budget()
    return {
        "conversation_id": conversation_id,
        "cleared": True,
        "budget_reset": budget,
        "session_cost_usd": round(agent.session_cost_usd, 4),
    }


@router.post("/briefing")
async def briefing(runtime: RuntimeDep) -> dict[str, Any]:
    """A short written summary of where things stand.

    A POST rather than a GET because it spends tokens: a GET that costs money
    is a trap for anything that prefetches.
    """
    agent = _assistant(runtime)
    answer = await agent.daily_briefing()
    return answer.as_dict()
