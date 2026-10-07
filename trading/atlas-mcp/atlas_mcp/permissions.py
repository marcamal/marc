"""The allowlist. This file is the whole security model of the MCP server.

ATLAS exposes a language model (yours, or OpenClaw's, or any MCP client) to a
brokerage account. The protection is not a prompt saying "please don't trade".
It is this list: the HTTP client refuses any request whose method and path are
not on it, so a tool that was never written cannot be called, and a tool that
was written cannot reach past the endpoint it declared.

The asymmetry is deliberate and is the most important thing here:

    You can STOP trading from your phone.
    You cannot START trading from your phone.

Engaging the kill switch is allowed because the cost of a false positive is a
halted paper account, and the cost of not being able to stop is unbounded.
Releasing it is not allowed, because that decision deserves a human in front
of the dashboard who can see the whole system state.

This module imports nothing but the standard library, so the backend's own
test suite can import it and assert these properties without installing the
MCP SDK.
"""

from __future__ import annotations

#: Exactly the requests the MCP server may make, as (method, path) pairs.
#:
#: Paths are matched by prefix for the ones ending in `/`, and exactly
#: otherwise - see `is_allowed`. Keep this list literal and boring: a regex or
#: a wildcard here is how a `/api/orders` slips in behind a `/api/or*`.
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        # --- system -------------------------------------------------------
        ("GET", "/api/system/status"),
        ("GET", "/api/system/health"),
        ("GET", "/api/system/mode"),
        ("GET", "/api/system/capabilities"),
        ("GET", "/api/system/kill-switch"),
        # --- account, read-only -------------------------------------------
        ("GET", "/api/account"),
        ("GET", "/api/positions"),
        ("GET", "/api/orders"),
        ("GET", "/api/portfolio"),
        ("GET", "/api/portfolio/equity-curve"),
        # --- market, read-only --------------------------------------------
        ("GET", "/api/market/clock"),
        ("GET", "/api/market/watchlist"),
        ("GET", "/api/market/bars/"),
        ("GET", "/api/market/quote/"),
        # --- research, read-only ------------------------------------------
        ("GET", "/api/scanner/results"),
        ("GET", "/api/agents"),
        ("GET", "/api/strategies"),
        ("GET", "/api/journal"),
        ("GET", "/api/mentor/explanations"),
        # --- risk ----------------------------------------------------------
        ("GET", "/api/risk/status"),
        ("GET", "/api/risk/decisions"),
        ("GET", "/api/risk/events"),
        # POST, but read-only by contract: /api/risk/check evaluates a
        # hypothetical proposal and returns the verdict. It does not publish a
        # decision and the Execution Agent never sees it, which is why the
        # backend has two separate methods on the Risk Agent - `check()` for
        # this, `evaluate()` for the real pipeline.
        ("POST", "/api/risk/check"),
        # --- the assistant --------------------------------------------------
        # Text in, text out. See backend/app/ai/base.py.
        ("POST", "/api/assistant/ask"),
        ("GET", "/api/assistant/status"),
        ("POST", "/api/assistant/briefing"),
        # --- the one permitted state change ---------------------------------
        ("POST", "/api/system/kill-switch/engage"),
    }
)

#: Requests the MCP server must never make, with the reason. Checked *before*
#: the allowlist, so adding something to `ALLOWED` by mistake cannot enable
#: one of these. Belt and braces, on purpose: this is the list where a
#: mistake costs money.
FORBIDDEN: dict[tuple[str, str], str] = {
    ("POST", "/api/system/kill-switch/release"): (
        "Releasing the kill switch means allowing trading again. That needs a "
        "human at the dashboard who can see the whole system state, not a chat "
        "message. Open the ATLAS dashboard and press Release Kill Switch."
    ),
    ("POST", "/api/orders"): (
        "Placing an order through this bridge is not possible. In ATLAS only the "
        "Execution Agent may send an order, and only on a decision published by "
        "the Risk Officer. There is no HTTP endpoint for it at all."
    ),
    ("DELETE", "/api/orders"): (
        "Cancelling an order is not available here. Use the dashboard, or engage "
        "the kill switch, which cancels every resting order at once."
    ),
    ("POST", "/api/strategies"): (
        "Enabling a strategy means allowing the system to generate trade "
        "proposals. That is a dashboard decision, not a chat one."
    ),
}

#: Path prefixes that are refused outright, whatever the method. A guard
#: against a future endpoint whose name alone marks it as dangerous.
FORBIDDEN_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/api/system/kill-switch/release", "the kill switch is released from the dashboard only"),
    ("/api/strategies/", "strategies are enabled and disabled from the dashboard only"),
)


class NotPermitted(PermissionError):
    """Raised when a request is outside the allowlist.

    Carries the reason, so the MCP client shows the operator *why* rather than
    a bare 403 - and so a model that asked for it learns the boundary instead
    of retrying.
    """


def _normalise(path: str) -> str:
    """Reduce a path to the form the allowlist is written in.

    Two jobs, and the second one matters more than it looks:

    1.  Drop the query string and fragment. They are not part of the
        permission decision - a limit or a symbol cannot turn a read into a
        write - and leaving them on would break every exact match.

    2.  **Resolve `.` and `..` segments.** Several allowlist entries match by
        prefix because they take a symbol in the path, and without this a
        request for

            /api/market/bars/../../orders

        would pass the `/api/market/bars/` prefix check and then be resolved
        by the server to `/api/orders`. Collapsing the path here means the
        allowlist decides on the path that will actually be requested.

    An attempt to climb above the root collapses to `/`, which is not on the
    allowlist, so it is refused rather than silently clamped.
    """
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path

    resolved: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not resolved:
                # Escaped the root. Return something that cannot match.
                return "/"
            resolved.pop()
            continue
        resolved.append(segment)

    collapsed = "/" + "/".join(resolved)
    # Preserve a meaningful trailing slash so a prefix entry still reads as a
    # prefix, but never invent one.
    if path.endswith("/") and collapsed != "/":
        collapsed += "/"
    return collapsed


def check(method: str, path: str) -> None:
    """Raise `NotPermitted` unless this request is explicitly allowed.

    Fails closed: anything not on the list is refused, including a path this
    code has never heard of.
    """
    verb = method.upper()
    clean = _normalise(path)

    reason = FORBIDDEN.get((verb, clean))
    if reason is not None:
        raise NotPermitted(reason)

    for prefix, why in FORBIDDEN_PREFIXES:
        if clean.startswith(prefix):
            raise NotPermitted(f"Not available through the MCP bridge: {why}.")

    if (verb, clean) in ALLOWED:
        return

    # Prefix entries, for the endpoints that take a symbol in the path.
    for allowed_verb, allowed_path in ALLOWED:
        if allowed_verb == verb and allowed_path.endswith("/") and clean.startswith(allowed_path):
            return

    raise NotPermitted(
        f"{verb} {clean} is not on the ATLAS MCP allowlist. This bridge is "
        f"read-only apart from engaging the kill switch. Anything that changes "
        f"the account - placing an order, changing a limit, enabling a "
        f"strategy, releasing the kill switch - is done from the dashboard."
    )


def is_allowed(method: str, path: str) -> bool:
    """Boolean form of `check`, for tests and for introspection tools."""
    try:
        check(method, path)
    except NotPermitted:
        return False
    return True


#: Shown to the MCP client as the server's instructions, and returned by the
#: `atlas_capabilities` tool. Stating the boundary plainly saves the model from
#: trying things that will fail, and tells the operator what they bought.
CAPABILITY_SUMMARY = """\
This bridge exposes a personal ATLAS trading system, running on the operator's
own machine in paper mode.

CAN DO
    Read the account: equity, cash, buying power, positions, open and filled
    orders, the equity curve.
    Read the system: trading mode, broker capabilities, agent health, the kill
    switch, configured and hard-coded risk limits.
    Read research: the scanner ranking, strategy state, the trade journal, the
    Mentor Agent's explanations, quotes and bars for a symbol.
    Evaluate a hypothetical trade against every risk rule, without placing it.
    Ask the ATLAS assistant a question in plain language.
    Engage the kill switch, which blocks all new orders and cancels resting
    ones.

CANNOT DO, by design and enforced in code
    Place, modify or cancel an order. In ATLAS only the Execution Agent may
    send an order, and only on a decision published by the Risk Officer.
    Change any risk limit. Those are deterministic code with hard-coded
    ceilings above the configuration.
    Enable or disable a strategy.
    Release the kill switch. Stopping is allowed from here; starting again
    requires a human at the dashboard.
    Read or transmit any API credential. This bridge talks to a local HTTP
    endpoint and never sees a key.

The asymmetry is the point: you can stop trading from your phone, you cannot
start it.
"""
