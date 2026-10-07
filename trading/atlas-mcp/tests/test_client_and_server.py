"""Tests for the HTTP client's guards and the tool surface it exposes.

No test here reaches the network: the allowlist refusals happen before a
socket is opened, which is itself one of the things asserted.
"""

from __future__ import annotations

import pytest

from atlas_mcp import permissions
from atlas_mcp.client import DEFAULT_BASE_URL, AtlasClient, _loopback_only

# --------------------------------------------------------------------------- #
# the bridge must stay local
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:9999/",
        "http://localhost",
    ],
)
def test_loopback_urls_are_accepted(url: str) -> None:
    assert _loopback_only(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.50:8000",
        "https://atlas.example.com",
        "http://10.0.0.1:8000",
        "http://169.254.169.254",  # cloud metadata, the classic SSRF target
        "http://evil.test:8000",
    ],
)
def test_remote_urls_are_refused(url: str) -> None:
    """A bridge pointed at an arbitrary host is an open proxy.

    It would sit in front of someone's brokerage dashboard with a language
    model driving it, so this is a hard failure rather than a warning.
    """
    with pytest.raises(ValueError, match="local machine"):
        _loopback_only(url)


def test_the_default_endpoint_is_loopback() -> None:
    assert DEFAULT_BASE_URL.startswith("http://127.0.0.1")


def test_the_base_url_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_MCP_BASE_URL", "http://localhost:9100")
    assert AtlasClient().base_url == "http://localhost:9100"


def test_a_remote_environment_variable_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAS_MCP_BASE_URL", "http://10.1.2.3:8000")
    with pytest.raises(ValueError):
        AtlasClient()


# --------------------------------------------------------------------------- #
# a refused request never opens a socket
# --------------------------------------------------------------------------- #


async def test_a_forbidden_request_is_refused_before_any_io() -> None:
    """The permission check runs before the HTTP client is even built.

    Asserting `_client is None` afterwards is what proves it: if the check ran
    after `_ensure()`, a forbidden request would still have constructed a
    client and resolved a connection.
    """
    client = AtlasClient("http://127.0.0.1:8000")

    with pytest.raises(permissions.NotPermitted):
        await client.post("/api/system/kill-switch/release")

    assert client._client is None
    assert client.requests == []
    assert client.refusals == ["POST /api/system/kill-switch/release"]


async def test_a_forbidden_order_is_refused_before_any_io() -> None:
    client = AtlasClient("http://127.0.0.1:8000")

    with pytest.raises(permissions.NotPermitted, match="Execution Agent"):
        await client.post("/api/orders", {"symbol": "SPY", "qty": 100})

    assert client._client is None


async def test_an_unreachable_backend_says_how_to_start_it() -> None:
    """Port 1 has nothing listening, so this exercises the real failure path."""
    from atlas_mcp.client import AtlasUnreachable

    client = AtlasClient("http://127.0.0.1:1")
    try:
        with pytest.raises(AtlasUnreachable, match="Is the backend"):
            await client.get("/api/system/health")
    finally:
        await client.aclose()


async def test_reachable_reports_rather_than_raises() -> None:
    client = AtlasClient("http://127.0.0.1:1")
    try:
        ok, message = await client.reachable()
    finally:
        await client.aclose()

    assert ok is False
    assert "not reachable" in message or "Could not reach" in message


# --------------------------------------------------------------------------- #
# the tool surface
# --------------------------------------------------------------------------- #


async def test_every_registered_tool_is_accounted_for() -> None:
    """The tool list is the bridge's public contract, so pin it.

    A new tool must be a deliberate decision, and adding one here forces a
    look at whether it belongs on a read-only bridge.
    """
    from atlas_mcp import server

    tools = {tool.name for tool in await server.mcp.list_tools()}

    expected = {
        # introspection
        "atlas_capabilities",
        # reads
        "atlas_status",
        "atlas_account",
        "atlas_positions",
        "atlas_portfolio",
        "atlas_orders",
        "atlas_scanner",
        "atlas_agents",
        "atlas_risk_limits",
        "atlas_risk_decisions",
        "atlas_market",
        "atlas_quote",
        "atlas_watchlist",
        "atlas_journal",
        "atlas_explanations",
        # read-only evaluation
        "atlas_risk_check",
        # the assistant
        "atlas_ask",
        "atlas_briefing",
        # the one state change
        "atlas_engage_kill_switch",
    }

    assert tools == expected, (
        f"the tool surface changed.\n"
        f"  added:   {sorted(tools - expected)}\n"
        f"  removed: {sorted(expected - tools)}"
    )


async def test_only_the_kill_switch_is_marked_destructive() -> None:
    """The annotations must tell the truth: a client gates on them."""
    from atlas_mcp import server

    destructive = set()
    for tool in await server.mcp.list_tools():
        annotations = tool.annotations
        if annotations is not None and getattr(annotations, "destructive_hint", None):
            destructive.add(tool.name)

    assert destructive == {"atlas_engage_kill_switch"}


async def test_no_tool_name_suggests_an_action_it_cannot_perform() -> None:
    """A tool called `atlas_buy` would be a lie the model would act on."""
    from atlas_mcp import server

    forbidden_words = ("buy", "sell", "order", "submit", "execute", "trade", "release", "enable")
    for tool in await server.mcp.list_tools():
        if tool.name == "atlas_orders":
            continue  # reads orders; the plural noun is accurate
        lowered = tool.name.lower()
        for word in forbidden_words:
            assert word not in lowered, (
                f"tool '{tool.name}' is named as if it could {word}. This bridge cannot."
            )


# --------------------------------------------------------------------------- #
# symbol validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("symbol", ["SPY", "spy", " qqq ", "BRK.B", "ABC-D"])
def test_valid_symbols_are_normalised(symbol: str) -> None:
    from atlas_mcp.server import _symbol

    assert _symbol(symbol) == symbol.strip().upper()


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        "   ",
        "../../orders",
        "SPY/../../orders",
        "A" * 20,
        "SPY?x=1",
        "SPY#frag",
        "SPY SPY",
        "SPY;rm -rf",
    ],
)
def test_invalid_symbols_are_rejected(symbol: str) -> None:
    """Belt and braces behind path normalisation: a crafted symbol never
    becomes a path segment in the first place."""
    from atlas_mcp.server import _symbol

    assert _symbol(symbol) is None
