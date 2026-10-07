"""ATLAS backend entry point.

Run it with:

    cd trading/backend
    uvicorn app.main:app --reload

or just:

    python -m app.main

Then open http://127.0.0.1:8000/docs for the interactive API, and run the
frontend separately for the dashboard.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import api_router, ws_router
from app.config.loader import ConfigError, get_config
from app.config.settings import LOGS_DIR, TradingMode, get_settings
from app.core.logging import configure_logging, get_logger
from app.runtime import AtlasRuntime

log = get_logger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the ATLAS runtime alongside the web server.

    A startup failure is deliberately *not* fatal to the HTTP server: the app
    still serves `/api/system/status`, so the operator can read the error in
    the dashboard instead of staring at a dead port and a stack trace.
    """
    settings = get_settings()
    runtime: AtlasRuntime | None = None

    try:
        runtime = AtlasRuntime(settings=settings)
        app.state.runtime = runtime
        await runtime.startup()
    except Exception as exc:
        log.exception("ATLAS runtime failed to start")
        app.state.startup_error = f"{type(exc).__name__}: {exc}"
        if runtime is not None:
            app.state.runtime = runtime

    try:
        yield
    finally:
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.shutdown()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    settings = get_settings()

    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        log_file=LOGS_DIR / "atlas.jsonl",
    )

    # Validate the YAML config before the server starts. A bad risk limit
    # should be a clear error on the first line of output, not a surprise
    # three hours into a session.
    try:
        get_config()
    except ConfigError as exc:
        log.critical("configuration error — ATLAS cannot start:\n%s", exc)
        raise

    mode = settings.effective_mode
    app = FastAPI(
        title="ATLAS",
        description=(
            "Personal AI trading and investment operating system.\n\n"
            f"**Current mode: {mode.value.upper()}**\n\n"
            "Live trading requires three independent environment flags and cannot "
            "be enabled through this API."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # The browser talks only to this backend; this backend talks to Alpaca.
    # Brokerage credentials never reach frontend JavaScript.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    app.include_router(ws_router)

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, Any]:
        """Service banner and a map of the useful endpoints."""
        startup_error = getattr(app.state, "startup_error", None)
        return {
            "name": "ATLAS",
            "version": "0.1.0",
            "mode": mode.value,
            "live": mode is TradingMode.LIVE,
            "startup_error": startup_error,
            "docs": "/docs",
            "endpoints": {
                "status": "/api/system/status",
                "health": "/api/system/health",
                "capabilities": "/api/system/capabilities",
                "kill_switch": "/api/system/kill-switch",
                "account": "/api/account",
                "positions": "/api/positions",
                "orders": "/api/orders",
                "portfolio": "/api/portfolio",
                "market_clock": "/api/market/clock",
                "watchlist": "/api/market/watchlist",
                "agents": "/api/agents",
                "agent_graph": "/api/agents/graph",
                "scanner": "/api/scanner/results",
                "strategies": "/api/strategies",
                "risk": "/api/risk/status",
                "risk_check": "POST /api/risk/check",
                "journal": "/api/journal",
                "assistant": "POST /api/assistant/ask",
                "assistant_status": "/api/assistant/status",
                "websocket": "/ws/events",
            },
        }

    @app.exception_handler(ConfigError)
    async def config_error_handler(_: Any, exc: ConfigError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": f"configuration error: {exc}"})

    return app


app = create_app()


def main() -> None:
    """Run the development server."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_config=None,  # keep ATLAS's own logging setup
    )


if __name__ == "__main__":
    main()
