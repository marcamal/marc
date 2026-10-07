"""Structured logging, plus the in-memory log buffer the dashboard reads.

Two things make this more than a `logging.basicConfig` call:

1.  **Trace ids.** Every decision chain — scanner finding a symbol, technical
    analysis scoring it, a strategy proposing a trade, risk approving it,
    execution submitting it — carries one `trace_id` through a `contextvar`.
    That is what makes "why did ATLAS buy this?" a query rather than an
    archaeology project. See `app.core.trace`.

2.  **A ring buffer.** The System page needs recent logs without reading files
    off disk on every poll, so records are also kept in memory.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.trace import get_trace_id

#: Fields already present on a LogRecord; anything else was added by the
#: caller via `extra=` and should be serialised as structured context.
_STANDARD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_LEVEL_COLOURS = {
    "DEBUG": "\033[38;5;245m",
    "INFO": "\033[38;5;39m",
    "WARNING": "\033[38;5;214m",
    "ERROR": "\033[38;5;203m",
    "CRITICAL": "\033[48;5;196m\033[38;5;231m",
}
_RESET = "\033[0m"


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_FIELDS and not key.startswith("_")
    }


class LogRecordBuffer(logging.Handler):
    """Keeps the most recent records in memory for `/api/system/logs`.

    Bounded, so a long-running session cannot grow without limit.
    """

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "trace_id": getattr(record, "trace_id", None) or get_trace_id(),
                "context": _extra_fields(record),
            }
            if record.exc_info:
                entry["exception"] = self.format(record)
            self._records.append(entry)
        except Exception:  # pragma: no cover - a logging handler must never raise
            self.handleError(record)

    def tail(self, limit: int = 100, min_level: str | None = None) -> list[dict[str, Any]]:
        """Most recent records first."""
        records = list(self._records)
        if min_level:
            threshold = logging.getLevelName(min_level.upper())
            if isinstance(threshold, int):
                records = [
                    r
                    for r in records
                    if isinstance(logging.getLevelName(r["level"]), int)
                    and logging.getLevelName(r["level"]) >= threshold
                ]
        return list(reversed(records[-limit:]))

    def clear(self) -> None:
        self._records.clear()


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Use this when you want to grep or ship logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = getattr(record, "trace_id", None) or get_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        extra = _extra_fields(record)
        if extra:
            payload["ctx"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Readable, aligned, colourised output for development."""

    def __init__(self, *, colour: bool = True) -> None:
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        level = record.levelname
        if self.colour:
            tint = _LEVEL_COLOURS.get(level, "")
            level_text = f"{tint}{level:<8}{_RESET}"
        else:
            level_text = f"{level:<8}"

        # Strip the "app." prefix: every logger has it, so it is pure noise.
        name = record.name.removeprefix("app.")
        line = f"{stamp} {level_text} {name:<28} {record.getMessage()}"

        trace_id = getattr(record, "trace_id", None) or get_trace_id()
        if trace_id:
            line += f"  [trace={trace_id[:8]}]"

        extra = _extra_fields(record)
        if extra:
            rendered = " ".join(f"{k}={v}" for k, v in extra.items())
            line += f"  {rendered}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


#: The single buffer instance the API reads from.
log_buffer = LogRecordBuffer()


def configure_logging(
    level: str = "INFO",
    fmt: str = "console",
    log_file: Path | None = None,
    buffer_capacity: int = 1000,
) -> None:
    """Install ATLAS's handlers on the root logger.

    Idempotent: calling it twice (uvicorn reload, tests) replaces the handlers
    rather than stacking them, which would duplicate every line.
    """
    global log_buffer

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(level.upper())

    console = logging.StreamHandler(stream=sys.stdout)
    if fmt == "json":
        console.setFormatter(JsonFormatter())
    else:
        console.setFormatter(ConsoleFormatter(colour=sys.stdout.isatty()))
    root.addHandler(console)

    log_buffer = LogRecordBuffer(capacity=buffer_capacity)
    log_buffer.setFormatter(JsonFormatter())
    root.addHandler(log_buffer)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # These libraries are chatty at INFO and drown out our own output.
    for noisy in ("httpx", "httpcore", "websockets", "urllib3", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Module-level logger. Use `get_logger(__name__)`."""
    return logging.getLogger(name)
