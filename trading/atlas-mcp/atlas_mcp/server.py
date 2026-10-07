"""The ATLAS MCP server: your trading system, reachable from any MCP client.

Point OpenClaw (or Claude Desktop, or any other MCP client) at this and you can
ask about your account from WhatsApp, Telegram or a terminal:

    "what is ATLAS holding right now?"
    "would a 10-share SPY long with a stop at 580 pass my risk rules?"
    "stop everything"

The last one works. The opposite does not: see `atlas_mcp.permissions` for the
asymmetry and why it is built that way.

Every tool here is a thin wrapper that names one allowlisted endpoint and
shapes the answer for a model to read. The tools do no arithmetic on money and
make no judgements - ATLAS already computed those, and a second implementation
here would be a second thing to get wrong.

Tool annotations are set honestly: `read_only_hint=True` on the reads, and
`destructive_hint=True` on the kill switch, so a well-behaved client can ask
the operator before firing it.
"""

from __future__ import annotations

import json
from typing import Any

from atlas_mcp import permissions
from atlas_mcp.client import AtlasClient, AtlasError, AtlasUnreachable

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as exc:  # pragma: no cover - depends on the install
    raise SystemExit(
        "The ATLAS MCP server needs the MCP Python SDK version 2 or newer "
        "(where FastMCP was renamed to MCPServer).\n\n"
        "    pip install -r atlas-mcp/requirements.txt\n\n"
        f"Import failed with: {exc}"
    ) from exc


mcp = MCPServer(
    name="atlas",
    title="ATLAS Trading System",
    version="0.1.0",
    instructions=permissions.CAPABILITY_SUMMARY,
)

#: One client for the process lifetime. Built lazily so importing this module
#: (which the tests do) neither opens a socket nor requires ATLAS to be up.
_client: AtlasClient | None = None


def client() -> AtlasClient:
    global _client
    if _client is None:
        _client = AtlasClient()
    return _client


def _dump(value: Any) -> str:
    """Serialise a payload for the model.

    JSON rather than prose: a model reads it reliably, and it keeps this file
    free of formatting decisions that would drift from the dashboard's.
    """
    return json.dumps(value, indent=2, default=str)


async def _safe(coro: Any, what: str) -> str:
    """Run a call and turn any expected failure into readable text.

    An MCP tool that raises gives the client a protocol error with no useful
    content. An explanation of what went wrong and what to do about it is far
    more use to whoever is reading the chat.
    """
    try:
        return _dump(await coro)
    except permissions.NotPermitted as exc:
        return f"Not permitted: {exc}"
    except AtlasUnreachable as exc:
        return f"ATLAS is not reachable: {exc}"
    except AtlasError as exc:
        return f"ATLAS could not answer the request for {what}: {exc}"


# --------------------------------------------------------------------------- #
# what this bridge is
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="atlas_capabilities",
    description=(
        "What this ATLAS bridge can and cannot do. Read this before assuming a "
        "trading action is possible - most are deliberately not."
    ),
    annotations={"read_only_hint": True, "open_world_hint": False},
)
async def atlas_capabilities() -> str:
    ok, message = await client().reachable()
    return (
        f"{permissions.CAPABILITY_SUMMARY}\n"
        f"Connection: {message}\n"
        f"Endpoint: {client().base_url}\n"
        f"Requests made this session: {len(client().requests)}\n"
        f"Requests refused this session: {len(client().refusals)}\n"
        f"ATLAS reachable: {ok}"
    )


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="atlas_status",
    description=(
        "The whole system at a glance: trading mode (paper or live), broker, "
        "agent health, kill switch, data feed, strategy state. Start here."
    ),
    annotations={"read_only_hint": True},
)
async def atlas_status() -> str:
    try:
        status = await client().get("/api/system/status")
    except (AtlasUnreachable, AtlasError) as exc:
        return f"ATLAS is not reachable: {exc}"

    # A trimmed view: the raw status payload is large and most of it is detail
    # a model does not need in order to answer "how are things".
    mode = status.get("mode", {})
    return _dump(
        {
            "mode": mode.get("effective"),
            "is_live": mode.get("is_live"),
            "real_money_at_risk": mode.get("is_live"),
            "mode_warnings": mode.get("warnings", []),
            "broker": status.get("broker", {}).get("name"),
            "simulated": status.get("broker", {}).get("simulated"),
            "market_open": (status.get("market_clock") or {}).get("is_open"),
            "kill_switch_engaged": status.get("kill_switch", {}).get("engaged"),
            "agents": status.get("agents", {}),
            "strategies": {
                "globally_enabled": status.get("strategies", {}).get("globally_enabled"),
                "active": status.get("strategies", {}).get("active"),
            },
            "orders_today": status.get("orders", {}).get("orders_today"),
            "uptime_seconds": status.get("uptime_seconds"),
            "startup_errors": status.get("startup_errors", []),
        }
    )


@mcp.tool(
    name="atlas_account",
    description="Equity, cash and buying power. Figures come from the broker, not an estimate.",
    annotations={"read_only_hint": True},
)
async def atlas_account() -> str:
    return await _safe(client().get("/api/account"), "the account")


@mcp.tool(
    name="atlas_positions",
    description="Every open position with its entry, current price and unrealised profit or loss.",
    annotations={"read_only_hint": True},
)
async def atlas_positions() -> str:
    return await _safe(client().get("/api/positions"), "positions")


@mcp.tool(
    name="atlas_portfolio",
    description=(
        "The portfolio snapshot: equity, exposure, unrealised and realised "
        "profit and loss. Use this for 'how am I doing'."
    ),
    annotations={"read_only_hint": True},
)
async def atlas_portfolio() -> str:
    return await _safe(client().get("/api/portfolio"), "the portfolio")


@mcp.tool(
    name="atlas_orders",
    description="Recent orders. `status` is one of open, closed or all.",
    annotations={"read_only_hint": True},
)
async def atlas_orders(status: str = "all", limit: int = 25) -> str:
    if status not in ("open", "closed", "all"):
        return "status must be one of: open, closed, all"
    return await _safe(
        client().get("/api/orders", status=status, limit=max(1, min(limit, 200))),
        "orders",
    )


@mcp.tool(
    name="atlas_scanner",
    description=(
        "The scanner's current ranking of the watchlist. An observation, never "
        "an instruction: a ranked symbol is not a trade until a strategy gives "
        "it an entry, a stop and a size and the Risk Officer approves it."
    ),
    annotations={"read_only_hint": True},
)
async def atlas_scanner(limit: int = 10) -> str:
    return await _safe(
        client().get("/api/scanner/results", limit=max(1, min(limit, 50))),
        "the scanner",
    )


@mcp.tool(
    name="atlas_agents",
    description="Every agent, its status and what it is doing right now.",
    annotations={"read_only_hint": True},
)
async def atlas_agents() -> str:
    try:
        payload = await client().get("/api/agents", log_limit=0)
    except (AtlasUnreachable, AtlasError) as exc:
        return f"ATLAS could not answer: {exc}"

    return _dump(
        {
            "health": payload.get("health", {}),
            "agents": [
                {
                    "id": agent.get("id"),
                    "name": agent.get("name"),
                    "role": agent.get("role"),
                    "status": agent.get("status"),
                    "current_task": agent.get("current_task"),
                }
                for agent in payload.get("agents", [])
            ],
        }
    )


@mcp.tool(
    name="atlas_risk_limits",
    description=(
        "The configured risk limits and the hard-coded ceilings above them. "
        "The ceilings are compiled into the code: configuration can only ever "
        "make ATLAS more conservative, never less."
    ),
    annotations={"read_only_hint": True},
)
async def atlas_risk_limits() -> str:
    try:
        payload = await client().get("/api/risk/status")
    except (AtlasUnreachable, AtlasError) as exc:
        return f"ATLAS could not answer: {exc}"

    return _dump(
        {
            "configured_limits": payload.get("config", {}),
            "hard_coded_ceilings": payload.get("hard_ceilings", {}),
            "kill_switch": payload.get("kill_switch", {}),
            "decisions_this_session": payload.get("agent", {}),
            "note": (
                "These limits are enforced by deterministic code in the Risk "
                "Agent, which has veto power over every order. No language "
                "model can relax one."
            ),
        }
    )


@mcp.tool(
    name="atlas_risk_decisions",
    description="Recent risk decisions, including what was blocked and which rule blocked it.",
    annotations={"read_only_hint": True},
)
async def atlas_risk_decisions(limit: int = 10) -> str:
    return await _safe(
        client().get("/api/risk/decisions", limit=max(1, min(limit, 100))),
        "risk decisions",
    )


@mcp.tool(
    name="atlas_market",
    description="Whether the US market is open, and when it next opens or closes.",
    annotations={"read_only_hint": True},
)
async def atlas_market() -> str:
    return await _safe(client().get("/api/market/clock"), "the market clock")


@mcp.tool(
    name="atlas_quote",
    description=(
        "The latest quote for one symbol, with its age. A stale quote is "
        "labelled as stale rather than presented as current."
    ),
    annotations={"read_only_hint": True},
)
async def atlas_quote(symbol: str) -> str:
    clean = _symbol(symbol)
    if clean is None:
        return f"{symbol!r} does not look like a stock symbol."
    return await _safe(client().get(f"/api/market/quote/{clean}"), f"a quote for {clean}")


@mcp.tool(
    name="atlas_watchlist",
    description="The configured watchlist with the latest price and change for each symbol.",
    annotations={"read_only_hint": True},
)
async def atlas_watchlist() -> str:
    return await _safe(client().get("/api/market/watchlist"), "the watchlist")


@mcp.tool(
    name="atlas_journal",
    description="The trade journal: decisions, their reasons and their outcomes.",
    annotations={"read_only_hint": True},
)
async def atlas_journal(limit: int = 10, search: str | None = None) -> str:
    return await _safe(
        client().get("/api/journal", limit=max(1, min(limit, 100)), search=search),
        "the journal",
    )


@mcp.tool(
    name="atlas_explanations",
    description=(
        "The Mentor Agent's explanations of recent events - why a trade was "
        "blocked, what a signal detected, what a fill means. These are "
        "deterministic templates built from real figures, not generated prose."
    ),
    annotations={"read_only_hint": True},
)
async def atlas_explanations(limit: int = 5) -> str:
    return await _safe(
        client().get("/api/mentor/explanations", limit=max(1, min(limit, 50))),
        "mentor explanations",
    )


# --------------------------------------------------------------------------- #
# the read-only risk check
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="atlas_risk_check",
    description=(
        "Run a hypothetical trade past every risk rule and report the verdict, "
        "WITHOUT placing it. This is the tool to use for 'could I buy X'. It "
        "returns which rules passed, which failed and why, and the position "
        "size the rules would allow. No order is created and nothing is sent "
        "to the broker."
    ),
    annotations={"read_only_hint": True, "destructive_hint": False},
)
async def atlas_risk_check(
    symbol: str,
    side: str,
    quantity: float,
    entry_price: float,
    stop_price: float,
    target_price: float | None = None,
) -> str:
    clean = _symbol(symbol)
    if clean is None:
        return f"{symbol!r} does not look like a stock symbol."
    if side.lower() not in ("buy", "sell"):
        return "side must be 'buy' or 'sell'."
    if quantity <= 0 or entry_price <= 0 or stop_price <= 0:
        return "quantity, entry_price and stop_price must all be positive."

    try:
        payload = await client().post(
            "/api/risk/check",
            {
                "symbol": clean,
                "side": side.lower(),
                "quantity": quantity,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "strategy_id": "mcp_check",
            },
        )
    except (AtlasUnreachable, AtlasError) as exc:
        return f"ATLAS could not run the check: {exc}"

    decision = payload.get("decision", {})
    failed = [c for c in decision.get("checks", []) if not c.get("passed")]

    return _dump(
        {
            "verdict": decision.get("decision"),
            "summary": payload.get("summary"),
            "approved_quantity": decision.get("approved_quantity"),
            "failed_rules": [
                {
                    "rule": check.get("rule"),
                    "detail": check.get("detail"),
                    "limit": check.get("limit"),
                    "observed": check.get("observed"),
                }
                for check in failed
            ],
            "checks_passed": len(decision.get("checks", [])) - len(failed),
            "note": (
                "Evaluation only. No order was placed and none can be placed "
                "through this bridge. To act on this, use the dashboard."
            ),
        }
    )


# --------------------------------------------------------------------------- #
# the assistant
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="atlas_ask",
    description=(
        "Ask the ATLAS assistant a question in plain language. It answers from "
        "the system's own measured state - the account, the risk decisions, the "
        "agents - and explains the reasoning. Good for 'why', 'explain' and "
        "'what should I look at'. It cannot act, only explain."
    ),
    annotations={"read_only_hint": True},
)
async def atlas_ask(question: str) -> str:
    if not question.strip():
        return "Ask something."
    try:
        payload = await client().post(
            "/api/assistant/ask",
            {"question": question, "conversation_id": "mcp"},
            timeout=150.0,
        )
    except (AtlasUnreachable, AtlasError) as exc:
        return f"ATLAS could not answer: {exc}"

    answer = payload.get("answer", "")
    if payload.get("degraded"):
        return f"[ATLAS assistant unavailable] {answer}"
    return answer


@mcp.tool(
    name="atlas_briefing",
    description=(
        "A short written briefing on where the account and the system stand: "
        "mode, holdings, anything blocked, any unhealthy agent, and what to "
        "look at next. Good as a morning or evening check-in."
    ),
    annotations={"read_only_hint": True},
)
async def atlas_briefing() -> str:
    try:
        payload = await client().post("/api/assistant/briefing", timeout=150.0)
    except (AtlasUnreachable, AtlasError) as exc:
        return f"ATLAS could not produce a briefing: {exc}"
    return payload.get("answer", "")


# --------------------------------------------------------------------------- #
# the one permitted state change
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="atlas_engage_kill_switch",
    description=(
        "EMERGENCY STOP. Blocks every new order and cancels every resting one, "
        "immediately. Use it when the operator says to stop, or when something "
        "is clearly wrong.\n\n"
        "This is the only action in this bridge that changes anything. It is "
        "allowed because the cost of stopping unnecessarily is a halted paper "
        "account, while the cost of being unable to stop is unbounded.\n\n"
        "It cannot be undone from here: releasing the kill switch is done at "
        "the dashboard by a human. Say so when you use it."
    ),
    annotations={
        "read_only_hint": False,
        "destructive_hint": True,
        "idempotent_hint": True,
        "open_world_hint": False,
    },
)
async def atlas_engage_kill_switch(reason: str) -> str:
    detail = reason.strip() or "engaged over the MCP bridge"
    try:
        payload = await client().post(
            "/api/system/kill-switch/engage",
            {"reason": f"[MCP] {detail}"},
        )
    except (AtlasUnreachable, AtlasError) as exc:
        return (
            f"COULD NOT ENGAGE THE KILL SWITCH: {exc}\n\n"
            f"Do not assume trading has stopped. Open the ATLAS dashboard and "
            f"press Kill Switch, or close the backend process."
        )

    return (
        "KILL SWITCH ENGAGED.\n"
        f"Reason recorded: {detail}\n"
        f"Resting orders cancelled: {payload.get('orders_canceled', 0)}\n"
        f"Positions flattened: {payload.get('positions_flattened', 0)}\n\n"
        "No new order can be placed until it is released, and it can only be "
        "released from the ATLAS dashboard - not from here."
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _symbol(raw: str) -> str | None:
    """Validate and normalise a symbol before it reaches a URL path.

    Belt and braces behind `permissions._normalise`, which already collapses
    `..`: this rejects anything that is not a plain ticker, so a crafted
    "symbol" cannot become a path at all.
    """
    cleaned = raw.strip().upper()
    if not cleaned or len(cleaned) > 12:
        return None
    if not all(char.isalnum() or char in ".-" for char in cleaned):
        return None
    return cleaned


def main() -> None:
    """Run the server on stdio, which is how an MCP client launches it."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
