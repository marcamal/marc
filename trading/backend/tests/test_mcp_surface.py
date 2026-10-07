"""The MCP bridge's allowlist, checked from the backend's own test suite.

`atlas-mcp` has its own tests, but it also has its own virtual environment,
and in practice nobody remembers to run a second suite. The property being
protected here is too important for that: these tests assert that the bridge
exposing ATLAS to a chat app cannot place a trade.

So this module imports `atlas_mcp.permissions` by path. That module imports
nothing but the standard library precisely so this is possible without
installing the MCP SDK into the backend's environment.

It also checks the other half of the pact, which the bridge's own tests
cannot: that every endpoint on the allowlist still exists in this backend, and
that no endpoint which changes the account has quietly appeared under a path
the allowlist would let through.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import api_router
from app.config.settings import PROJECT_ROOT
from app.runtime import AtlasRuntime

#: trading/atlas-mcp/atlas_mcp/permissions.py
PERMISSIONS_PATH = PROJECT_ROOT / "atlas-mcp" / "atlas_mcp" / "permissions.py"


def _load_permissions() -> Any:
    """Import the bridge's allowlist module directly from its file.

    By path rather than by package name: `atlas-mcp` is not installed in the
    backend's environment and should not be, because the bridge is meant to
    run as an isolated process.
    """
    if not PERMISSIONS_PATH.exists():
        pytest.skip(f"atlas-mcp is not present at {PERMISSIONS_PATH}")

    spec = importlib.util.spec_from_file_location("_atlas_mcp_permissions", PERMISSIONS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered under a private name so it cannot shadow a real install.
    sys.modules["_atlas_mcp_permissions"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def permissions() -> Any:
    return _load_permissions()


# --------------------------------------------------------------------------- #
# the bridge cannot trade
# --------------------------------------------------------------------------- #


@pytest.mark.adversarial
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/orders"),
        ("POST", "/api/orders/submit"),
        ("DELETE", "/api/orders/abc"),
        ("POST", "/api/system/kill-switch/release"),
        ("POST", "/api/strategies/ema_vwap_momentum_v1/enable"),
        ("POST", "/api/system/mode"),
        ("PUT", "/api/risk/status"),
        # Traversal, which is how a prefix allowlist usually fails.
        ("POST", "/api/market/bars/../../orders"),
        ("POST", "/api/market/quote/SPY/../../../system/kill-switch/release"),
    ],
)
def test_the_mcp_bridge_cannot_reach_a_mutating_endpoint(
    permissions: Any, method: str, path: str
) -> None:
    assert not permissions.is_allowed(method, path), (
        f"the MCP bridge would allow {method} {path}. That bridge is reachable "
        f"from a chat app, so this must stay impossible."
    )


@pytest.mark.adversarial
def test_the_mcp_bridge_can_stop_but_not_start(permissions: Any) -> None:
    """The asymmetry, asserted from the backend side too."""
    assert permissions.is_allowed("POST", "/api/system/kill-switch/engage")
    assert not permissions.is_allowed("POST", "/api/system/kill-switch/release")


@pytest.mark.adversarial
def test_the_mcp_allowlist_has_exactly_one_mutating_entry(permissions: Any) -> None:
    read_only_posts = {
        "/api/risk/check",
        "/api/assistant/ask",
        "/api/assistant/briefing",
    }
    mutating = {
        path
        for method, path in permissions.ALLOWED
        if method != "GET" and path not in read_only_posts
    }
    assert mutating == {"/api/system/kill-switch/engage"}, (
        f"unexpected mutating endpoint(s) on the MCP allowlist: {mutating}"
    )


# --------------------------------------------------------------------------- #
# the pact with this backend
# --------------------------------------------------------------------------- #


@pytest.fixture
def spec(runtime: AtlasRuntime) -> dict[str, Any]:
    """The backend's own OpenAPI paths."""
    app = FastAPI(title="ATLAS mcp surface test")
    app.include_router(api_router)
    app.state.runtime = runtime
    with TestClient(app) as client:
        return client.get("/openapi.json").json()["paths"]


def test_every_allowlisted_endpoint_still_exists(permissions: Any, spec: dict[str, Any]) -> None:
    """A renamed endpoint must break loudly, not silently stop working.

    Without this, renaming `/api/scanner/results` would leave the bridge
    reporting "ATLAS returned 404" to the operator's phone forever, and
    nothing in either test suite would notice.
    """
    missing: list[str] = []

    for method, path in sorted(permissions.ALLOWED):
        if path.endswith("/"):
            # A prefix entry: the real route carries a path parameter, so
            # match on the prefix instead of the literal string.
            if not any(known.startswith(path) for known in spec):
                missing.append(f"{method} {path}*")
            continue
        operations = spec.get(path)
        if operations is None or method.lower() not in operations:
            missing.append(f"{method} {path}")

    assert not missing, (
        f"the MCP bridge allowlists endpoints this backend no longer serves: "
        f"{missing}. Either restore them or update "
        f"atlas-mcp/atlas_mcp/permissions.py."
    )


@pytest.mark.adversarial
def test_no_mutating_backend_endpoint_is_reachable_through_the_bridge(
    permissions: Any, spec: dict[str, Any]
) -> None:
    """Walk the real API and check the allowlist against all of it.

    The parametrised test above covers the paths someone thought of. This one
    covers the ones nobody thought of, including an endpoint added next month:
    every non-GET operation this backend serves is checked, and only the kill
    switch engage may be reachable.
    """
    reachable: list[str] = []

    for path, operations in spec.items():
        for method in operations:
            verb = method.upper()
            if verb in ("GET", "HEAD", "OPTIONS"):
                continue
            if permissions.is_allowed(verb, path):
                reachable.append(f"{verb} {path}")

    allowed_writes = {
        "POST /api/system/kill-switch/engage",
        # Read-only by contract; see the allowlist's comments.
        "POST /api/risk/check",
        "POST /api/assistant/ask",
        "POST /api/assistant/briefing",
    }
    unexpected = set(reachable) - allowed_writes

    assert not unexpected, (
        f"these non-GET endpoints are reachable through the MCP bridge and "
        f"were not vetted: {sorted(unexpected)}. Either add them to the "
        f"bridge's FORBIDDEN list or justify them here."
    )


def test_the_capability_summary_matches_the_allowlist(permissions: Any) -> None:
    """The text shown to a model must not promise what the list refuses."""
    summary = permissions.CAPABILITY_SUMMARY

    assert "Engage the kill switch" in summary
    assert "Release the kill switch" in summary  # as a cannot-do
    assert "Place, modify or cancel an order" in summary

    # And the claim about credentials must be true: no allowlisted endpoint
    # returns one. The context endpoint is the only one that could, and it is
    # deliberately absent from the bridge.
    assert not permissions.is_allowed("GET", "/api/assistant/context")
