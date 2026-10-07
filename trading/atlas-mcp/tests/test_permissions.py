"""Tests for the MCP bridge's allowlist.

This file is the reason the allowlist can be trusted. It asserts the shape of
the boundary rather than the behaviour of any one tool:

*   everything that would change the account is refused
*   the kill switch can be engaged but not released
*   path traversal cannot turn a read into a write
*   a request this code has never heard of is refused, not allowed

A failure here is not a cosmetic test break. It means the bridge can do
something it was built not to do.
"""

from __future__ import annotations

import pytest

from atlas_mcp import permissions
from atlas_mcp.permissions import ALLOWED, FORBIDDEN, NotPermitted, check, is_allowed

# --------------------------------------------------------------------------- #
# the things that must never be possible
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path"),
    [
        # Placing an order, in every shape someone might try.
        ("POST", "/api/orders"),
        ("PUT", "/api/orders"),
        ("POST", "/api/orders/submit"),
        ("POST", "/api/execution/submit"),
        ("POST", "/api/trade"),
        # Cancelling or modifying.
        ("DELETE", "/api/orders"),
        ("DELETE", "/api/orders/abc123"),
        ("PATCH", "/api/orders/abc123"),
        # Turning the safety off.
        ("POST", "/api/system/kill-switch/release"),
        ("DELETE", "/api/system/kill-switch"),
        # Enabling a strategy, which is what lets proposals be generated.
        ("POST", "/api/strategies/ema_vwap_momentum_v1/enable"),
        ("POST", "/api/strategies/ema_vwap_momentum_v1/disable"),
        ("POST", "/api/strategies"),
        # Changing risk.
        ("POST", "/api/risk/status"),
        ("PUT", "/api/risk/config"),
        ("PATCH", "/api/risk/limits"),
        # Switching to live trading.
        ("POST", "/api/system/mode"),
        ("PUT", "/api/system/mode"),
    ],
)
def test_dangerous_requests_are_refused(method: str, path: str) -> None:
    assert not is_allowed(method, path)
    with pytest.raises(NotPermitted):
        check(method, path)


def test_the_kill_switch_asymmetry() -> None:
    """Stopping is allowed. Starting again is not. This is the core design."""
    assert is_allowed("POST", "/api/system/kill-switch/engage")
    assert not is_allowed("POST", "/api/system/kill-switch/release")


def test_releasing_the_kill_switch_explains_where_to_do_it() -> None:
    """A refusal that teaches beats a refusal that stonewalls."""
    with pytest.raises(NotPermitted, match="dashboard"):
        check("POST", "/api/system/kill-switch/release")


def test_order_refusal_explains_the_architecture() -> None:
    with pytest.raises(NotPermitted, match="Execution Agent"):
        check("POST", "/api/orders")


# --------------------------------------------------------------------------- #
# fail closed
# --------------------------------------------------------------------------- #


def test_an_unknown_path_is_refused() -> None:
    assert not is_allowed("GET", "/api/something/nobody/wrote/yet")


def test_an_unknown_method_on_an_allowed_path_is_refused() -> None:
    """Reading positions is fine. Writing to the same path is not."""
    assert is_allowed("GET", "/api/positions")
    assert not is_allowed("POST", "/api/positions")
    assert not is_allowed("DELETE", "/api/positions")
    assert not is_allowed("PUT", "/api/positions")


def test_the_root_is_refused() -> None:
    assert not is_allowed("GET", "/")
    assert not is_allowed("GET", "/api")


def test_the_openapi_spec_is_not_exposed() -> None:
    """No need, and it is a map of everything the bridge is refusing."""
    assert not is_allowed("GET", "/openapi.json")
    assert not is_allowed("GET", "/docs")


# --------------------------------------------------------------------------- #
# path traversal: the hole a prefix allowlist invites
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/api/market/bars/../../orders",
        "/api/market/quote/../../../api/orders",
        "/api/market/bars/./../../orders",
        "/api/market/quote/SPY/../../../orders",
    ],
)
def test_traversal_cannot_smuggle_a_write_past_a_prefix_entry(path: str) -> None:
    """`/api/market/bars/` matches by prefix because it takes a symbol.

    Without `..` resolution each of these would satisfy that prefix check and
    then be resolved by the *server* to `/api/orders` - so a POST would reach
    the order endpoint while the allowlist believed it had approved a read of
    a price series.

    The decision is made on the resolved path, which is the property asserted
    here. Note the deliberate asymmetry in the two assertions: a GET that
    resolves to `/api/orders` is *allowed*, because reading orders is allowed
    and the path it resolves to is the one that gets requested. Refusing it
    would be theatre. What must not happen is a write getting through.
    """
    assert not is_allowed("POST", path)
    assert not is_allowed("DELETE", path)
    assert is_allowed("GET", path), "the resolved path is a permitted read"


@pytest.mark.parametrize(
    "path",
    [
        "/api/market/bars/../../system/kill-switch/release",
        "/api/market/quote/SPY/../../../system/kill-switch/release",
        "/api/market/bars/./../../strategies/x/enable",
    ],
)
def test_traversal_onto_a_forbidden_path_is_refused(path: str) -> None:
    """Resolution must land the request on the forbidden list, not past it."""
    assert not is_allowed("POST", path)
    assert not is_allowed("GET", path)


def test_traversal_above_the_root_is_refused() -> None:
    assert not is_allowed("GET", "/api/../../../etc/passwd")


def test_normalisation_resolves_segments() -> None:
    assert permissions._normalise("/api/market/bars/../../orders") == "/api/orders"
    assert permissions._normalise("/api/positions?limit=5") == "/api/positions"
    assert permissions._normalise("/api/positions#frag") == "/api/positions"
    assert permissions._normalise("/api//positions") == "/api/positions"
    assert permissions._normalise("/../..") == "/"


# --------------------------------------------------------------------------- #
# the reads that must work
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/api/system/status",
        "/api/system/health",
        "/api/account",
        "/api/positions",
        "/api/orders",
        "/api/portfolio",
        "/api/scanner/results",
        "/api/agents",
        "/api/risk/status",
        "/api/risk/decisions",
        "/api/market/clock",
        "/api/journal",
        "/api/mentor/explanations",
    ],
)
def test_reads_are_allowed(path: str) -> None:
    assert is_allowed("GET", path)


@pytest.mark.parametrize("symbol", ["SPY", "QQQ", "BRK.B", "NVDA"])
def test_symbol_paths_are_allowed(symbol: str) -> None:
    assert is_allowed("GET", f"/api/market/quote/{symbol}")
    assert is_allowed("GET", f"/api/market/bars/{symbol}")


def test_a_query_string_does_not_change_the_decision() -> None:
    assert is_allowed("GET", "/api/orders?status=open&limit=10")
    assert not is_allowed("POST", "/api/orders?status=open")


def test_the_read_only_risk_check_is_allowed() -> None:
    """A POST, but it evaluates rather than acts - see the backend's two paths."""
    assert is_allowed("POST", "/api/risk/check")


def test_the_assistant_is_allowed() -> None:
    assert is_allowed("POST", "/api/assistant/ask")
    assert is_allowed("POST", "/api/assistant/briefing")
    assert is_allowed("GET", "/api/assistant/status")


# --------------------------------------------------------------------------- #
# properties of the list itself
# --------------------------------------------------------------------------- #


def test_the_allowlist_has_exactly_one_mutating_entry() -> None:
    """A regression guard with teeth.

    Every POST on the list must either be the kill-switch engage or an
    endpoint that is read-only by contract. If someone adds a mutating POST,
    this test names it.
    """
    read_only_posts = {
        "/api/risk/check",  # evaluates a hypothetical; publishes nothing
        "/api/assistant/ask",  # text in, text out
        "/api/assistant/briefing",  # text in, text out
    }
    mutating = {path for method, path in ALLOWED if method != "GET" and path not in read_only_posts}
    assert mutating == {"/api/system/kill-switch/engage"}, (
        f"unexpected mutating endpoint(s) on the allowlist: {mutating}. "
        f"Everything that changes the account belongs on the dashboard, not "
        f"on this bridge."
    )


def test_no_entry_is_both_allowed_and_forbidden() -> None:
    """A contradiction here would be resolved in favour of refusal, but it
    would mean the author of the list was confused about something important."""
    assert not (set(FORBIDDEN) & ALLOWED)


def test_every_forbidden_entry_has_a_reason() -> None:
    for (method, path), reason in FORBIDDEN.items():
        assert reason.strip(), f"{method} {path} is forbidden with no explanation"
        assert len(reason) > 40, f"{method} {path}: the reason should teach, not just refuse"


def test_the_allowlist_contains_no_wildcards() -> None:
    """A regex or a `*` in this list is how a write slips in behind a read."""
    for _, path in ALLOWED:
        assert "*" not in path
        assert "?" not in path
        assert "{" not in path


def test_the_capability_summary_states_both_halves() -> None:
    summary = permissions.CAPABILITY_SUMMARY
    assert "CAN DO" in summary
    assert "CANNOT DO" in summary
    for claim in (
        "Place, modify or cancel an order",
        "Release the kill switch",
        "Change any risk limit",
        "Enable or disable a strategy",
    ):
        assert claim in summary, f"the summary does not mention: {claim}"
