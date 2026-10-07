"""The global kill switch.

Two independent representations, deliberately:

1.  **A file on disk** (`data/KILL_SWITCH`). Creating that file stops all
    trading, and it works when the API is down, the UI is broken, or the
    process is wedged. You can stop ATLAS from a file manager. On Windows:
    right-click in the `data` folder, New > Text Document, name it
    `KILL_SWITCH`. Delete it to resume.

2.  **An in-process flag**, for programmatic triggers (daily loss breached,
    reconciliation mismatch, repeated rejections) and the dashboard button.

Either one being set blocks trading. The file is checked on every call rather
than cached, because the whole point is that it works without ATLAS's
cooperation.

What engaging does, in order:
    - block all new orders (always, not configurable)
    - cancel resting orders (configurable, default on)
    - flatten positions (configurable, default OFF)

Flattening defaults to off on purpose. Market-selling a whole book during a
data outage or a flash dislocation converts a paper problem into a realised
loss. The operator can turn it on in `risk.yaml` once they know they want it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.brokers.base import BrokerAdapter, BrokerError
from app.config.schema import KillSwitchConfig
from app.config.settings import PROJECT_ROOT
from app.core.logging import get_logger
from app.events.bus import EventBus
from app.events.types import KillSwitchEvent

log = get_logger(__name__)


class KillSwitch:
    """Blocks all order submission when engaged."""

    def __init__(
        self,
        config: KillSwitchConfig,
        bus: EventBus | None = None,
        project_root: Path | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        root = project_root or PROJECT_ROOT
        self._file = root / config.file_path

        self._engaged_in_memory = False
        self.reason: str | None = None
        self.triggered_by: str | None = None
        self.engaged_at: datetime | None = None
        #: Consecutive broker rejections, for the auto-trigger.
        self._rejection_streak = 0

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #

    @property
    def file_path(self) -> Path:
        return self._file

    @property
    def file_present(self) -> bool:
        """Checked live, every time. Never cached — see the module docstring."""
        try:
            return self._file.exists()
        except OSError:
            # If we cannot even stat the path, assume the worst and stop
            # trading. Fail closed.
            log.error("cannot check kill switch file at %s; failing closed", self._file)
            return True

    @property
    def is_engaged(self) -> bool:
        return self._engaged_in_memory or self.file_present

    def status(self) -> dict[str, object]:
        return {
            "engaged": self.is_engaged,
            "engaged_in_memory": self._engaged_in_memory,
            "file_present": self.file_present,
            "file_path": str(self._file),
            "reason": self.reason,
            "triggered_by": self.triggered_by,
            "engaged_at": self.engaged_at.isoformat() if self.engaged_at else None,
            "rejection_streak": self._rejection_streak,
            "will_cancel_orders": self._config.cancel_open_orders,
            "will_flatten_positions": self._config.flatten_positions,
            "auto_triggers": self._config.auto_triggers,
        }

    # ------------------------------------------------------------------ #
    # engage / release
    # ------------------------------------------------------------------ #

    async def engage(
        self,
        reason: str,
        triggered_by: str = "manual",
        broker: BrokerAdapter | None = None,
        write_file: bool = True,
    ) -> KillSwitchEvent:
        """Stop all trading.

        Idempotent: engaging an already-engaged switch refreshes the reason but
        does not cancel or flatten a second time.
        """
        already = self.is_engaged

        self._engaged_in_memory = True
        self.reason = reason
        self.triggered_by = triggered_by
        self.engaged_at = self.engaged_at or datetime.now(UTC)

        if write_file:
            self._write_file(reason, triggered_by)

        canceled = 0
        flattened = 0

        if not already and broker is not None:
            if self._config.cancel_open_orders:
                try:
                    canceled = await broker.cancel_all_orders()
                except BrokerError as exc:
                    # Log loudly but keep going: being unable to cancel must
                    # not prevent the switch from blocking new orders.
                    log.error("kill switch could not cancel open orders: %s", exc)

            if self._config.flatten_positions:
                log.critical("kill switch is configured to FLATTEN POSITIONS — selling everything")
                try:
                    flattened = await broker.close_all_positions(cancel_orders=False)
                except BrokerError as exc:
                    log.error("kill switch could not flatten positions: %s", exc)

        log.critical(
            "KILL SWITCH ENGAGED",
            extra={
                "reason": reason,
                "triggered_by": triggered_by,
                "orders_canceled": canceled,
                "positions_flattened": flattened,
            },
        )

        event = KillSwitchEvent(
            engaged=True,
            reason=reason,
            triggered_by=triggered_by,
            orders_canceled=canceled,
            positions_flattened=flattened,
            source="kill_switch",
        )
        if self._bus is not None:
            self._bus.publish_nowait(event)
        return event

    async def release(self, released_by: str = "manual") -> KillSwitchEvent:
        """Resume trading.

        Removes the file as well as the in-memory flag, otherwise the switch
        would appear stuck from the operator's point of view.
        """
        self._engaged_in_memory = False
        previous_reason = self.reason
        self.reason = None
        self.triggered_by = None
        self.engaged_at = None
        self._rejection_streak = 0

        if self.file_present:
            try:
                self._file.unlink()
            except OSError as exc:
                log.error("could not delete kill switch file %s: %s", self._file, exc)

        log.warning(
            "kill switch released",
            extra={"released_by": released_by, "previous_reason": previous_reason},
        )

        event = KillSwitchEvent(
            engaged=False,
            reason=f"released by {released_by}",
            triggered_by=released_by,
            source="kill_switch",
        )
        if self._bus is not None:
            self._bus.publish_nowait(event)
        return event

    def _write_file(self, reason: str, triggered_by: str) -> None:
        """Persist the switch so a restart does not silently resume trading."""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                f"ATLAS KILL SWITCH\n"
                f"engaged_at: {datetime.now(UTC).isoformat()}\n"
                f"triggered_by: {triggered_by}\n"
                f"reason: {reason}\n"
                f"\n"
                f"Delete this file to allow trading again.\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log.error("could not write kill switch file %s: %s", self._file, exc)

    # ------------------------------------------------------------------ #
    # automatic triggers
    # ------------------------------------------------------------------ #

    def _auto_enabled(self, trigger: str) -> bool:
        return bool(self._config.auto_triggers.get(trigger, False))

    async def check_auto_trigger(
        self,
        trigger: str,
        reason: str,
        broker: BrokerAdapter | None = None,
    ) -> bool:
        """Engage if `trigger` is enabled in `risk.yaml`. Returns True if engaged."""
        if self.is_engaged:
            return True
        if not self._auto_enabled(trigger):
            log.warning(
                "risk condition hit but its auto-trigger is disabled",
                extra={"trigger": trigger, "reason": reason},
            )
            return False
        await self.engage(reason=reason, triggered_by=f"auto:{trigger}", broker=broker)
        return True

    async def record_order_rejection(self, broker: BrokerAdapter | None = None) -> bool:
        """Count a broker rejection and engage if they keep coming.

        Repeated rejections usually mean a systematic problem — wrong symbol
        format, insufficient buying power, a closed market — and an automated
        system will otherwise retry forever.
        """
        self._rejection_streak += 1
        threshold = self._config.repeated_rejection_threshold
        if self._rejection_streak < threshold:
            return False
        return await self.check_auto_trigger(
            "repeated_order_rejections",
            f"{self._rejection_streak} consecutive order rejections "
            f"(threshold {threshold}). Something is systematically wrong.",
            broker=broker,
        )

    def record_order_success(self) -> None:
        """Reset the rejection streak after a successful submission."""
        self._rejection_streak = 0
