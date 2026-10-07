"""Decision traces: one id threaded through a whole chain of reasoning.

The goal is to be able to answer, after the fact:

    WHY DID ATLAS BUY NVDA?

    scanner      found NVDA, rank 3, relative volume 2.1
    technical    score 84, EMA stack bullish, above VWAP
    strategy     proposal: long 4 @ 178.20, stop 175.10, target 184.40
    risk         APPROVED, risk 0.21% of equity
    execution    submitted market buy, client_order_id atlas-...
    broker       acknowledged, filled 4 @ 178.23

Every one of those steps logs with the same `trace_id`, so the whole chain can
be retrieved with a single filter. Because the id lives in a `contextvar`, it
propagates through `await` calls automatically, without being passed as an
argument through every function in the stack.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_trace_id: ContextVar[str | None] = ContextVar("atlas_trace_id", default=None)


def new_trace_id(prefix: str = "") -> str:
    """A fresh short id. The prefix makes raw logs easier to skim."""
    token = uuid.uuid4().hex[:16]
    return f"{prefix}-{token}" if prefix else token


def get_trace_id() -> str | None:
    """The trace id for the current async task, if one is set."""
    return _trace_id.get()


def set_trace_id(trace_id: str | None) -> Token[str | None]:
    """Set the trace id directly. Prefer the `trace` context manager."""
    return _trace_id.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    _trace_id.reset(token)


@contextmanager
def trace(trace_id: str | None = None, prefix: str = "") -> Iterator[str]:
    """Run a block under a trace id, restoring the previous one afterwards.

        with trace(prefix="scan") as tid:
            log.info("scanning", extra={"symbols": 30})

    Nesting is safe: an inner block that passes an explicit id overrides the
    outer one only for its own duration.
    """
    resolved = trace_id or new_trace_id(prefix)
    token = _trace_id.set(resolved)
    try:
        yield resolved
    finally:
        _trace_id.reset(token)
