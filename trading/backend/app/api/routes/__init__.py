"""API route modules, aggregated into one router."""

from fastapi import APIRouter

from app.api.routes import (
    account,
    agents,
    assistant,
    journal,
    market,
    risk,
    scanner,
    strategies,
    system,
    ws,
)

#: Everything under /api.
api_router = APIRouter(prefix="/api")
api_router.include_router(system.router)
api_router.include_router(account.router)
api_router.include_router(market.router)
api_router.include_router(agents.router)
api_router.include_router(scanner.router)
api_router.include_router(strategies.router)
api_router.include_router(risk.router)
api_router.include_router(journal.router)
api_router.include_router(assistant.router)

#: The websocket sits outside /api, at /ws/events.
ws_router = ws.router

__all__ = ["api_router", "ws_router"]
