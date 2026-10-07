"""The HTTP client, with the allowlist wired in front of every request.

There is one method that performs IO - `_request` - and it calls
`permissions.check()` before touching the network. Every tool goes through it.
That is the invariant worth protecting: adding a tool cannot widen what the
bridge can reach, because a tool can only name an endpoint, and the endpoint
is checked.

The client talks to ATLAS over plain HTTP on localhost. It deliberately does
*not* import the ATLAS backend:

*   It cannot reach past the API into the broker, the risk engine or the
    event bus, because it has no reference to any of them.
*   It inherits every guard the API already enforces, instead of
    reimplementing them.
*   It can run as a separate process under a different user, which is how
    OpenClaw will launch it.
"""

from __future__ import annotations

import os
from typing import Any

from atlas_mcp import permissions

try:  # The MCP SDK pulls in httpx2; a standalone install may have httpx.
    import httpx
except ImportError:  # pragma: no cover - depends on the install
    import httpx2 as httpx  # type: ignore[import-not-found, no-redef]

#: Where the ATLAS backend listens by default. Loopback only: this bridge is
#: not a reverse proxy and must not be pointed at a public host.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: ATLAS is a local process, so a slow response means something is wrong
#: rather than that the internet is far away. The assistant endpoint is the
#: exception - it waits on a model - and gets its own timeout.
DEFAULT_TIMEOUT = 15.0
ASSISTANT_TIMEOUT = 150.0


class AtlasUnreachable(RuntimeError):
    """ATLAS is not running, or not listening where we looked."""


class AtlasError(RuntimeError):
    """ATLAS answered with an error. The message is the backend's own."""


def _loopback_only(base_url: str) -> str:
    """Refuse a non-local base URL.

    A bridge that an MCP client can point at an arbitrary host is an open
    proxy with the operator's brokerage dashboard behind it. ATLAS runs on
    the same machine; there is no legitimate reason for this to be remote,
    so the check is a hard failure rather than a warning.
    """
    cleaned = base_url.rstrip("/")
    host = cleaned.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        raise ValueError(
            f"ATLAS_MCP_BASE_URL must point at the local machine, got {host!r}. "
            f"This bridge is not a remote proxy: it exposes a brokerage "
            f"dashboard to whatever MCP client is connected, so it only ever "
            f"talks to 127.0.0.1."
        )
    return cleaned


class AtlasClient:
    """A narrow, allowlisted HTTP client for the ATLAS API."""

    def __init__(self, base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = _loopback_only(
            base_url or os.environ.get("ATLAS_MCP_BASE_URL") or DEFAULT_BASE_URL
        )
        self.timeout = timeout
        self._client: Any | None = None
        #: Every request made this session, for the `atlas_capabilities` tool
        #: and for debugging. Useful when something says "ATLAS refused".
        self.requests: list[str] = []
        self.refusals: list[str] = []

    async def _ensure(self) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    # the one place that performs IO
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Make one allowlisted request.

        The permission check comes first, before the client is even built, so
        a forbidden request never opens a socket.
        """
        try:
            permissions.check(method, path)
        except permissions.NotPermitted:
            self.refusals.append(f"{method} {path}")
            raise

        self.requests.append(f"{method} {path}")
        client = await self._ensure()

        try:
            response = await client.request(
                method,
                path,
                params=params,
                json=json_body,
                timeout=timeout or self.timeout,
            )
        except Exception as exc:
            raise AtlasUnreachable(
                f"Could not reach ATLAS at {self.base_url}. Is the backend "
                f"running? Start it with trading/scripts/Start-ATLAS.ps1 on "
                f"Windows, or `uvicorn app.main:app` in trading/backend. "
                f"({type(exc).__name__}: {exc})"
            ) from exc

        if response.status_code >= 400:
            detail: str
            try:
                body = response.json()
                detail = body.get("detail") or response.text
            except Exception:
                detail = response.text
            raise AtlasError(f"ATLAS returned {response.status_code}: {detail}")

        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", path, params=clean or None)

    async def post(
        self, path: str, body: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        return await self._request("POST", path, json_body=body, timeout=timeout)

    # ------------------------------------------------------------------ #
    # health
    # ------------------------------------------------------------------ #

    async def reachable(self) -> tuple[bool, str]:
        """Is ATLAS up? Returns `(ok, message)` rather than raising."""
        try:
            health = await self.get("/api/system/health")
        except AtlasUnreachable as exc:
            return False, str(exc)
        except AtlasError as exc:
            return False, str(exc)
        return True, f"ATLAS is up ({health.get('status', 'unknown')})"
