"""Assembles the read-only snapshot of ATLAS that a model is allowed to see.

This module is the security boundary. Everything a language model knows about
the account arrives through here, as plain text, in one direction. There is no
return path: the model's output is text that the UI renders, and nothing in
ATLAS parses it as a command.

Three rules the code enforces:

1.  **Read-only, in-memory only.** Every figure comes from state ATLAS already
    computed - the portfolio snapshot the Portfolio Agent reconciles, the
    scanner's last ranking, the agents' descriptors. No broker call is made
    while building a context, so asking a question cannot move the account and
    cannot be used to hammer the broker's rate limit.

2.  **No secrets.** API keys, the database URL and file paths never appear. A
    model that cannot see a credential cannot leak one.

3.  **Sectioned format.** Lines beginning `## ` open a section. That is a
    contract with `app.ai.null_provider`, which answers questions by looking
    up sections when no model is configured - so the format is tested, not
    incidental.

Budget: the whole context is capped (`MAX_CONTEXT_CHARS`) and each section is
truncated rather than dropped, so a 200-position account cannot silently push
the mode banner or the risk limits out of the prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.config.settings import TradingMode
from app.risk.rules import (
    ABSOLUTE_MAX_DAILY_LOSS_PCT,
    ABSOLUTE_MAX_LEVERAGE,
    ABSOLUTE_MAX_POSITION_PCT,
    ABSOLUTE_MAX_RISK_PER_TRADE_PCT,
)

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime

#: Roughly 12k characters is a few thousand tokens: generous for a personal
#: account, cheap to send on every turn, and small enough that the cached
#: system prompt stays the dominant part of the request.
MAX_CONTEXT_CHARS = 12_000

#: Per-section cap, so one huge section cannot crowd out the others.
MAX_SECTION_CHARS = 2_600

#: How many rows of each list to include.
MAX_POSITIONS = 25
MAX_SCANNER_ROWS = 12
MAX_DECISIONS = 8
MAX_ORDERS = 10


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def _truncate(body: str, limit: int = MAX_SECTION_CHARS) -> str:
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "\n... (truncated)"


class ContextBuilder:
    """Builds the text snapshot. One instance per runtime; stateless per call."""

    def __init__(self, runtime: AtlasRuntime) -> None:
        self.runtime = runtime

    # ------------------------------------------------------------------ #
    # public
    # ------------------------------------------------------------------ #

    def build(self, *, include: set[str] | None = None) -> str:
        """The full context, or only the named sections.

        `include` is lowercase section names. The mode section is always
        present: a model must never discuss the account without knowing
        whether real money is involved.
        """
        builders: list[tuple[str, Any]] = [
            ("mode", self._mode),
            ("account", self._account),
            ("positions", self._positions),
            ("risk", self._risk),
            ("recent risk decisions", self._decisions),
            ("recent orders", self._orders),
            ("scanner", self._scanner),
            ("strategies", self._strategies),
            ("agents", self._agents),
            ("market", self._market),
        ]

        chunks: list[str] = [
            f"Snapshot taken at {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC."
        ]
        used = len(chunks[0])

        for name, builder in builders:
            if include is not None and name != "mode" and name not in include:
                continue
            try:
                body = _truncate(builder().strip())
            except Exception as exc:  # pragma: no cover - defensive
                # A broken section must not take down the assistant. Say that
                # the data is missing; never silently omit it, because a model
                # reading a context with no POSITIONS heading would reasonably
                # conclude there are no positions.
                body = f"(unavailable: {type(exc).__name__}: {exc})"

            section = f"\n## {name.upper()}\n{body}\n"
            if used + len(section) > MAX_CONTEXT_CHARS:
                chunks.append("\n## NOTE\n(further sections omitted: context budget reached)\n")
                break
            chunks.append(section)
            used += len(section)

        return "".join(chunks)

    # ------------------------------------------------------------------ #
    # sections
    # ------------------------------------------------------------------ #

    def _mode(self) -> str:
        settings = self.runtime.settings
        mode = settings.effective_mode
        broker = self.runtime.broker

        lines = [
            f"Trading mode: {mode.value.upper()}"
            + (
                "  <-- REAL MONEY IS AT RISK"
                if mode is TradingMode.LIVE
                else "  (no real money involved)"
            ),
            f"Broker: {broker.name}"
            + (" (simulated data, no account)" if broker.name == "simulated" else ""),
            f"Requested mode: {settings.requested_mode.value}",
            "Live trading requires three independent environment flags; any "
            "ambiguity resolves to paper.",
        ]
        if mode is not TradingMode.LIVE:
            closed = [name for name, open_ in settings.live_gates.items() if not open_]
            if closed:
                lines.append(f"Live gates currently closed: {', '.join(closed)}")

        capabilities = self.runtime.capabilities
        if capabilities is not None:
            unsupported = capabilities.unsupported_summary()
            if unsupported:
                lines.append(
                    "NOT available on this account (do not suggest these): "
                    + ", ".join(unsupported)
                )
            lines.append(
                f"Market data feed: {capabilities.data_feed} "
                f"(paid plan: {capabilities.has_paid_data_plan}), "
                f"max {capabilities.max_stream_symbols} streaming symbols"
            )
            if not capabilities.detected:
                lines.append("Capability detection failed, so conservative defaults are in use.")

        lines.append(
            "The operator lives in Germany. Do not assume US-only products, "
            "accounts or tax treatment are available to them."
        )
        return "\n".join(lines)

    def _account(self) -> str:
        snapshot = self.runtime.portfolio_snapshot
        if snapshot is None:
            return (
                "No portfolio snapshot yet. The Portfolio Agent takes one on its "
                "first cycle after startup."
            )

        lines = [
            f"Equity: {_money(snapshot.equity)}",
            f"Cash: {_money(snapshot.cash)}",
            f"Buying power: {_money(snapshot.buying_power)}",
            f"Open positions: {len(snapshot.positions)}",
            f"Total exposure: {_money(snapshot.total_exposure)} "
            f"({snapshot.total_exposure_pct:.1f}% of equity)",
            f"Unrealised P&L: {_money(snapshot.unrealized_pl)}",
            f"Realised P&L today: {_money(snapshot.realized_pl_today)}",
            f"As of: {snapshot.as_of.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        ]
        return "\n".join(lines)

    def _positions(self) -> str:
        snapshot = self.runtime.portfolio_snapshot
        if snapshot is None:
            return "Unknown - no portfolio snapshot yet."
        if not snapshot.positions:
            return "No open positions. The account is entirely in cash."

        lines = ["symbol  side   qty      entry      last       value        unrealised P&L"]
        for position in snapshot.positions[:MAX_POSITIONS]:
            lines.append(
                f"{position.symbol:<7} {position.side.value:<6} "
                f"{position.quantity:<8.4g} "
                f"{position.average_entry_price:<10.2f} "
                f"{(position.current_price or 0.0):<10.2f} "
                f"{_money(position.market_value):<12} "
                f"{_money(position.unrealized_pl)} "
                f"({_pct(position.unrealized_pl_pct)})"
            )
        if len(snapshot.positions) > MAX_POSITIONS:
            lines.append(f"... and {len(snapshot.positions) - MAX_POSITIONS} more")
        return "\n".join(lines)

    def _risk(self) -> str:
        risk = self.runtime.config.risk
        kill = self.runtime.kill_switch

        lines = [
            "Configured limits (from config/risk.yaml):",
            f"  max risk per trade: {risk.max_risk_per_trade_pct}% of equity",
            f"  max position size: {risk.max_position_pct}% of equity",
            f"  max total exposure: {risk.max_total_exposure_pct}% of equity",
            f"  max daily loss: {risk.max_daily_loss_pct}% of equity",
            f"  max portfolio drawdown: {risk.max_portfolio_drawdown_pct}%",
            f"  max open positions: {risk.max_open_positions}",
            f"  max orders per day: {risk.max_orders_per_day}",
            f"  min reward:risk: {risk.min_reward_risk_ratio}:1",
            f"  max leverage: {risk.max_leverage}x",
            "",
            "Hard-coded ceilings in code, which config cannot exceed:",
            f"  risk per trade <= {ABSOLUTE_MAX_RISK_PER_TRADE_PCT}%",
            f"  position size <= {ABSOLUTE_MAX_POSITION_PCT}%",
            f"  daily loss <= {ABSOLUTE_MAX_DAILY_LOSS_PCT}%",
            f"  leverage <= {ABSOLUTE_MAX_LEVERAGE}x",
            "",
            f"Kill switch: {'ENGAGED - all new orders blocked' if kill.is_engaged else 'not engaged'}",
        ]
        if kill.is_engaged and kill.reason:
            lines.append(f"  reason: {kill.reason}")

        agent = self._agent("risk")
        if agent is not None:
            detail = agent.detail()
            lines += [
                "",
                f"Risk decisions this session: {detail.get('decisions_made', 0)} "
                f"({detail.get('approvals', 0)} approved, "
                f"{detail.get('rejections', 0)} rejected, "
                f"{detail.get('reductions', 0)} size-reduced)",
            ]
        return "\n".join(lines)

    def _decisions(self) -> str:
        agent = self._agent("risk")
        decisions = getattr(agent, "recent_decisions", None) if agent else None
        if not decisions:
            return "No risk decisions yet this session."

        lines = []
        for decision in decisions[:MAX_DECISIONS]:
            failed = [c for c in decision.checks if not c.passed]
            summary = (
                "; ".join(f"{c.rule}: {c.detail}" for c in failed[:3])
                if failed
                else "all checks passed"
            )
            lines.append(
                f"{decision.decided_at.strftime('%H:%M:%S')} "
                f"{decision.decision.value.upper()} "
                f"(proposal {decision.proposal_id}) - {summary}"
            )
        return "\n".join(lines)

    def _orders(self) -> str:
        tracker = self.runtime.order_tracker
        orders = tracker.all_orders()[:MAX_ORDERS]
        status = tracker.status()

        lines = [
            f"Orders today: {status.get('orders_today', 0)} (open: {status.get('open_orders', 0)})"
        ]
        if not orders:
            lines.append("No orders submitted this session.")
            return "\n".join(lines)

        for order in orders:
            when = order.submitted_at.strftime("%H:%M:%S") if order.submitted_at else "--:--:--"
            fill = (
                f" @ {order.average_fill_price:.2f}" if order.average_fill_price is not None else ""
            )
            lines.append(
                f"{when} {order.side.value} {order.quantity:g} {order.symbol} "
                f"{order.order_type.value} -> {order.status.value}{fill}"
            )
        return "\n".join(lines)

    def _scanner(self) -> str:
        agent = self._agent("scanner")
        results = getattr(agent, "results", None) if agent else None
        if not results:
            return (
                "The scanner has not produced a ranking yet. It runs on its own "
                "interval once the Scanner Agent is started."
            )

        lines = [
            "Ranked candidates (an observation, never an instruction):",
            "rank symbol  price     change   rel.vol  RSI    ATR%   score",
        ]
        for result in results[:MAX_SCANNER_ROWS]:
            lines.append(
                f"{result.rank:<4} {result.symbol:<7} "
                f"{(result.price or 0.0):<9.2f} "
                f"{_pct(result.percent_change):<8} "
                f"{(result.relative_volume or 0.0):<8.2f} "
                f"{(result.rsi or 0.0):<6.1f} "
                f"{(result.atr_percent or 0.0):<6.2f} "
                f"{result.score:.3f}"
            )
        return "\n".join(lines)

    def _strategies(self) -> str:
        registry = self.runtime.strategies
        status = registry.status()
        lines = [
            f"Strategies globally enabled: {status.get('globally_enabled')}",
            f"Loaded: {status.get('loaded', 0)}, active: {status.get('active', 0)}",
        ]
        for strategy in registry.all:
            lines.append(
                f"  {strategy.id}: {'ENABLED' if strategy.enabled else 'disabled'} "
                f"- symbols {', '.join(strategy.symbols[:8]) or 'none'}"
            )
        if not registry.active:
            lines.append("Nothing can generate a trade proposal while no strategy is active.")
        return "\n".join(lines)

    def _agents(self) -> str:
        manager = self.runtime.agents
        if manager is None:
            return "The agent manager is not initialised."

        health = manager.health_summary()
        lines = [
            f"{health.get('running', 0)} of {health.get('registered', 0)} agents "
            f"running ({health.get('unhealthy', 0)} unhealthy)",
        ]
        for agent in manager.agents:
            lines.append(
                f"  {agent.id:<14} {agent.status.value:<10} {agent.current_task or 'idle'}"
            )
        return "\n".join(lines)

    def _market(self) -> str:
        clock = self.runtime.market_clock
        lines: list[str] = []
        if clock is None:
            lines.append("Market clock unknown - the Market Data Agent has not polled yet.")
        else:
            lines.append(f"US market is {'OPEN' if clock.is_open else 'CLOSED'}")
            if clock.next_open:
                lines.append(f"Next open: {clock.next_open.isoformat()}")
            if clock.next_close:
                lines.append(f"Next close: {clock.next_close.isoformat()}")

        service = self.runtime.data_service
        if service is not None:
            status = service.status()
            lines.append(f"Data feed: {status.get('feed')}")
            lines.append(f"Streaming symbols: {status.get('symbol_count', 0)}")
            states = status.get("connection_states") or {}
            if states:
                lines.append(
                    "Connections: " + ", ".join(f"{name}={state}" for name, state in states.items())
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _agent(self, agent_type: str) -> Any | None:
        """Find a running agent by its declared type.

        By type rather than by id, because ids come from `agents.yaml` and the
        operator may rename them.
        """
        manager = self.runtime.agents
        if manager is None:
            return None
        found = manager.by_type(agent_type)
        return found[0] if found else None
