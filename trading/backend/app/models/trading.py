"""Account, order, position, signal and trade-proposal models.

`TradeProposal` is the most important type in ATLAS. It is the only thing a
strategy may produce, and the only thing the Risk Agent will consider. Its
validators are a first line of defence: a proposal that is internally
incoherent (stop on the wrong side of entry, zero quantity, no strategy id)
cannot be constructed at all, so it can never reach the broker.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.models.enums import (
    OrderStatus,
    OrderType,
    PositionSide,
    RiskDecisionType,
    Side,
    SignalDirection,
    TimeInForce,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# broker state
# --------------------------------------------------------------------------- #


class AccountSnapshot(BaseModel):
    """The broker's view of the account. Always authoritative."""

    account_id: str
    currency: str = "USD"
    equity: float
    last_equity: float = 0.0
    cash: float
    buying_power: float
    #: Only meaningful on a margin account. 1.0 on a cash account.
    multiplier: float = 1.0
    portfolio_value: float = 0.0
    daytrade_count: int = 0
    pattern_day_trader: bool = False
    trading_blocked: bool = False
    transfers_blocked: bool = False
    account_blocked: bool = False
    shorting_enabled: bool = False
    crypto_enabled: bool = False
    options_enabled: bool = False
    as_of: datetime = Field(default_factory=_utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def daily_pl(self) -> float:
        """P&L since the previous session's close."""
        if self.last_equity <= 0:
            return 0.0
        return self.equity - self.last_equity

    @computed_field  # type: ignore[prop-decorator]
    @property
    def daily_pl_pct(self) -> float:
        if self.last_equity <= 0:
            return 0.0
        return (self.daily_pl / self.last_equity) * 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_restricted(self) -> bool:
        """Any broker-side block that should stop ATLAS trading."""
        return self.trading_blocked or self.account_blocked


class Position(BaseModel):
    """An open position as the broker reports it."""

    symbol: str
    quantity: float
    side: PositionSide
    average_entry_price: float
    current_price: float | None = None
    market_value: float = 0.0
    cost_basis: float = 0.0
    unrealized_pl: float = 0.0
    unrealized_pl_pct: float = 0.0
    asset_class: str = "us_equity"
    #: Which ATLAS strategy opened this, when known. Positions opened by hand
    #: in the Alpaca UI have no strategy, which is itself useful to display.
    strategy_id: str | None = None
    as_of: datetime = Field(default_factory=_utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exposure(self) -> float:
        """Absolute market value — what the risk limits actually care about."""
        return abs(self.market_value)


class Fill(BaseModel):
    """A single execution against an order."""

    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    timestamp: datetime = Field(default_factory=_utcnow)


class Order(BaseModel):
    """Normalised order. Mirrors the broker, not our intent."""

    id: str
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    status: OrderStatus
    quantity: float
    filled_quantity: float = 0.0
    limit_price: float | None = None
    stop_price: float | None = None
    average_fill_price: float | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    canceled_at: datetime | None = None
    expired_at: datetime | None = None
    #: Broker-supplied reason, present on rejections.
    reason: str | None = None
    #: Links the order back to the proposal and the decision chain.
    proposal_id: str | None = None
    strategy_id: str | None = None
    trace_id: str | None = None
    as_of: datetime = Field(default_factory=_utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.status.is_open


# --------------------------------------------------------------------------- #
# research output
# --------------------------------------------------------------------------- #


class Evidence(BaseModel):
    """One concrete reason behind a signal.

    Structured rather than free text so the dashboard can render it and the
    Mentor Agent can explain it without parsing prose.
    """

    source: str
    detail: str
    value: float | str | bool | None = None
    weight: float = 1.0


class Signal(BaseModel):
    """An agent's structured opinion about one symbol.

    This is the `AgentMessage` shape from the project brief: agents exchange
    these, not free-form text.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    agent_id: str
    symbol: str
    direction: SignalDirection
    confidence: float = Field(ge=0.0, le=1.0)
    timeframe: str = "5Min"
    created_at: datetime = Field(default_factory=_utcnow)
    evidence: list[Evidence] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    #: 0-100 convenience score for display and ranking.
    score: float = 0.0
    trace_id: str | None = None
    notes: str = ""


# --------------------------------------------------------------------------- #
# trade proposal
# --------------------------------------------------------------------------- #


class TradeProposal(BaseModel):
    """A strategy's request to open a position.

    A strategy may ONLY produce one of these. It may never call the broker.
    The flow is:

        Strategy -> TradeProposal -> Risk Agent -> Portfolio Agent
                 -> Execution Agent -> broker

    Validation here is intentionally strict. Every rule below exists because
    the alternative is a malformed order reaching a live account.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"prop-{uuid.uuid4().hex[:12]}")
    strategy_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    quantity: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    target_price: float | None = Field(default=None, gt=0)

    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: float | None = Field(default=None, gt=0)

    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    #: What would prove this thesis wrong. Required reading for the journal.
    invalidation: str = ""

    created_at: datetime = Field(default_factory=_utcnow)
    #: After this moment the proposal is stale and must be re-derived from
    #: fresh data rather than executed at an old price.
    expires_at: datetime | None = None
    trace_id: str | None = None

    @model_validator(mode="after")
    def _validate_coherence(self) -> TradeProposal:
        # --- stop must be on the losing side of entry ----------------------
        # A "stop" above entry on a long is not a stop, it is a target. Getting
        # this backwards would send a stop that triggers instantly.
        if self.side is Side.BUY and self.stop_price >= self.entry_price:
            raise ValueError(
                f"long proposal needs stop_price < entry_price "
                f"(got stop={self.stop_price}, entry={self.entry_price})"
            )
        if self.side is Side.SELL and self.stop_price <= self.entry_price:
            raise ValueError(
                f"short proposal needs stop_price > entry_price "
                f"(got stop={self.stop_price}, entry={self.entry_price})"
            )

        # --- target, when present, must be on the winning side -------------
        if self.target_price is not None:
            if self.side is Side.BUY and self.target_price <= self.entry_price:
                raise ValueError("long proposal needs target_price > entry_price")
            if self.side is Side.SELL and self.target_price >= self.entry_price:
                raise ValueError("short proposal needs target_price < entry_price")

        # --- limit orders need a limit price -------------------------------
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError(f"{self.order_type.value} proposal requires limit_price")

        # --- timestamps must be timezone-aware ------------------------------
        # A naive datetime compared against an aware one raises at runtime,
        # typically inside the staleness check. Catch it at construction.
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")

        return self

    # ------------------------------------------------------------------ #
    # derived risk arithmetic
    # ------------------------------------------------------------------ #

    @computed_field  # type: ignore[prop-decorator]
    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def estimated_risk(self) -> float:
        """Money lost if the stop is hit exactly, ignoring slippage."""
        return self.risk_per_share * self.quantity

    @computed_field  # type: ignore[prop-decorator]
    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reward_risk_ratio(self) -> float | None:
        """Reward divided by risk. `None` when no target is set."""
        if self.target_price is None or self.risk_per_share <= 0:
            return None
        reward = abs(self.target_price - self.entry_price)
        return reward / self.risk_per_share

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or _utcnow()) - self.created_at).total_seconds()

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or _utcnow()) >= self.expires_at

    def with_quantity(self, quantity: float) -> TradeProposal:
        """A copy at a different size, for risk-driven downsizing.

        Returns a new object: the Risk Agent must never mutate the proposal it
        was handed, so that the audit trail keeps what the strategy asked for
        alongside what was actually approved.
        """
        return self.model_copy(update={"quantity": quantity})

    @classmethod
    def expiring_in(cls, seconds: int, **kwargs: object) -> TradeProposal:
        """Convenience constructor that sets `expires_at` relative to now."""
        created = kwargs.pop("created_at", None) or _utcnow()
        return cls(
            created_at=created,  # type: ignore[arg-type]
            expires_at=created + timedelta(seconds=seconds),  # type: ignore[operator]
            **kwargs,  # type: ignore[arg-type]
        )


class RiskCheckResult(BaseModel):
    """The outcome of one named risk rule."""

    rule: str
    passed: bool
    detail: str = ""
    #: The limit that applied and the value measured, for the audit trail.
    limit: float | None = None
    observed: float | None = None


class RiskDecision(BaseModel):
    """The Risk Agent's verdict. Binding on the Execution Agent."""

    proposal_id: str
    decision: RiskDecisionType
    checks: list[RiskCheckResult] = Field(default_factory=list)
    #: Set when the decision is APPROVED_REDUCED.
    approved_quantity: float | None = None
    reasons: list[str] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=_utcnow)
    trace_id: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_approved(self) -> bool:
        return self.decision in (
            RiskDecisionType.APPROVED,
            RiskDecisionType.APPROVED_REDUCED,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed_rules(self) -> list[str]:
        return [c.rule for c in self.checks if not c.passed]

    def summary(self) -> str:
        if self.is_approved:
            note = "approved"
            if self.decision is RiskDecisionType.APPROVED_REDUCED:
                note = f"approved, reduced to {self.approved_quantity}"
            return note
        return f"rejected: {'; '.join(self.reasons) or 'no reason recorded'}"


class PortfolioSnapshot(BaseModel):
    """A point-in-time view of the whole book."""

    as_of: datetime = Field(default_factory=_utcnow)
    equity: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0
    positions: list[Position] = Field(default_factory=list)
    total_exposure: float = 0.0
    total_exposure_pct: float = 0.0
    unrealized_pl: float = 0.0
    realized_pl_today: float = 0.0
    daily_pl: float = 0.0
    daily_pl_pct: float = 0.0
    #: Highest equity ever seen, used for the drawdown calculation.
    high_water_mark: float = 0.0
    drawdown_pct: float = 0.0
    open_position_count: int = 0
    exposure_by_correlation_group: dict[str, float] = Field(default_factory=dict)
    exposure_by_strategy: dict[str, float] = Field(default_factory=dict)
    #: True when ATLAS's record and the broker's disagree. Broker wins; this
    #: flag exists so the disagreement is visible rather than papered over.
    reconciliation_mismatch: bool = False
    mismatch_detail: list[str] = Field(default_factory=list)
