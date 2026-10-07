"""Offline broker and market data, for development and tests.

Why this exists:

*   **ATLAS must run before credentials exist.** You can explore the dashboard,
    start agents, watch the pipeline and trigger the kill switch with an empty
    `.env`. Nothing silently pretends to be real: the UI shows `SIMULATED`.
*   **Tests must never place a brokerage order.** Every test runs against this
    class. No test in the suite can reach the network.

Prices come from a seeded random walk, so a given symbol produces the same
series on every run. Reproducible tests matter more than realistic prices, and
nothing here is a substitute for a backtest on real historical data.
"""

from __future__ import annotations

import hashlib
import math
import random
import uuid
from datetime import UTC, datetime, timedelta

from app.brokers.base import (
    BrokerAdapter,
    BrokerCapabilities,
    DuplicateOrder,
    MarketDataProvider,
    OrderRejected,
    OrderRequest,
)
from app.core.logging import get_logger
from app.market_data import timeframes
from app.models.enums import (
    AssetClass,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)
from app.models.market import Bar, MarketClock, Quote, Snapshot, Trade
from app.models.trading import AccountSnapshot, Order, Position

log = get_logger(__name__)

#: Rough starting prices so the simulation is not wildly unrealistic.
#: Anything not listed gets a deterministic price derived from its name.
_SEED_PRICES: dict[str, float] = {
    "SPY": 585.0,
    "QQQ": 505.0,
    "IWM": 228.0,
    "VOO": 538.0,
    "VXUS": 66.0,
    "BND": 73.0,
    "GLD": 248.0,
    "NVDA": 178.0,
    "AMD": 142.0,
    "AVGO": 232.0,
    "MSFT": 428.0,
    "AAPL": 232.0,
    "GOOGL": 184.0,
    "AMZN": 212.0,
    "META": 586.0,
    "TSLA": 342.0,
    "SMH": 262.0,
    "VRT": 118.0,
    "TT": 392.0,
    "CARR": 72.0,
    "JCI": 84.0,
    "ETN": 328.0,
    "BTC/USD": 94000.0,
    "ETH/USD": 3300.0,
}


def _timeframe_minutes(timeframe: str) -> int:
    """Minutes per bar, using the same validation as the real adapter.

    Shared deliberately: the simulator used to fall back to 5 minutes for an
    unrecognised timeframe, so a typo worked offline and failed against
    Alpaca. Now both raise.
    """
    return timeframes.minutes(timeframe)


def _base_price(symbol: str) -> float:
    """Stable pseudo-price for a symbol we have no seed for."""
    if symbol in _SEED_PRICES:
        return _SEED_PRICES[symbol]
    digest = hashlib.sha256(symbol.encode()).digest()
    return 20.0 + (digest[0] << 8 | digest[1]) % 48000 / 100.0


class SimulatedMarketData(MarketDataProvider):
    """Deterministic synthetic bars, quotes and snapshots."""

    name = "simulated"

    #: Minutes in a trading day, the reference period for `volatility`.
    _DAILY_MINUTES = 1440

    def __init__(self, seed: int = 20260101, daily_volatility: float = 0.012) -> None:
        """
        Args:
            seed: makes every series reproducible.
            daily_volatility: standard deviation of a *daily* return, so 0.012
                is a 1.2% day. Shorter timeframes are scaled down from this.
                It is defined per day rather than per bar because that is the
                number a person can sanity-check: a 1.2% day is normal, while
                a 1.2% five-minute bar compounds to roughly 20% a day.
        """
        self._seed = seed
        self._volatility = daily_volatility

    def _rng(self, symbol: str, salt: str = "") -> random.Random:
        """One RNG per (symbol, purpose), so series are independent but stable."""
        return random.Random(f"{self._seed}:{symbol}:{salt}")

    def _series(self, symbol: str, count: int, timeframe: str = "") -> list[float]:
        """A seeded random walk ending at roughly the symbol's seed price.

        The walk is generated forward and then rescaled so the *last* value is
        the seed price. Without that anchor, a few hundred steps of compounding
        drift leave SPY quoted at 716 instead of 585 — harmless for ranking
        maths, but confusing when reading the dashboard and misleading when
        eyeballing whether a position size looks sane.

        `timeframe` salts the RNG, and higher timeframes get proportionally
        larger per-bar moves. Without this, the 5-minute and daily series were
        numerically identical, which made multi-timeframe analysis look like it
        worked while actually testing nothing.
        """
        rng = self._rng(symbol, f"walk:{timeframe}")
        base = _base_price(symbol)

        # Scale the daily volatility to this bar length by the square root of
        # time, as diffusion implies: a 5-minute bar moves far less than a
        # daily one. Anchoring at a *day* keeps the numbers sane — scaling up
        # from a 5-minute reference instead produced 20% daily candles and a
        # dashboard showing SPY up 41% in a session.
        bar_minutes = _timeframe_minutes(timeframe) if timeframe else 5
        period = bar_minutes / self._DAILY_MINUTES
        vol = self._volatility * math.sqrt(period)
        # A mild upward drift, scaled to the same bar length so it stays in
        # proportion to the noise rather than dominating short timeframes.
        drift = 0.0003 * period

        price = base
        prices: list[float] = []
        for _ in range(count):
            shock = rng.gauss(0.0, vol)
            price = max(0.5, price * math.exp(drift + shock))
            prices.append(price)

        # Rescale so the series ends at the seed price, preserving its shape.
        scale = base / prices[-1] if prices[-1] > 0 else 1.0
        return [round(p * scale, 2) for p in prices]

    async def get_bars(
        self,
        symbols: list[str],
        timeframe: str = "5Min",
        limit: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, list[Bar]]:
        minutes = _timeframe_minutes(timeframe)
        anchor = end or datetime.now(UTC)
        out: dict[str, list[Bar]] = {}

        for symbol in symbols:
            closes = self._series(symbol, limit, timeframe=timeframe)
            rng = self._rng(symbol, f"bars:{timeframe}")
            bars: list[Bar] = []
            for index, close in enumerate(closes):
                # Oldest first, which is what every indicator here expects.
                ts = anchor - timedelta(minutes=minutes * (limit - index - 1))
                open_ = closes[index - 1] if index else close * (1 - rng.uniform(0, 0.003))
                high = max(open_, close) * (1 + rng.uniform(0, 0.004))
                low = min(open_, close) * (1 - rng.uniform(0, 0.004))
                volume = rng.uniform(400_000, 3_000_000)
                bars.append(
                    Bar(
                        symbol=symbol,
                        timestamp=ts,
                        open=round(open_, 2),
                        high=round(high, 2),
                        low=round(low, 2),
                        close=close,
                        volume=round(volume),
                        trade_count=int(volume / 180),
                        vwap=round((high + low + close) / 3, 2),
                    )
                )
            out[symbol] = bars
        return out

    async def get_latest_quote(self, symbol: str) -> Quote | None:
        return (await self.get_latest_quotes([symbol])).get(symbol)

    async def get_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        now = datetime.now(UTC)
        out: dict[str, Quote] = {}
        for symbol in symbols:
            # Anchored at the seed price, so quotes agree with the bar series.
            mid = _base_price(symbol)
            rng = self._rng(symbol, "quote")
            half_spread = max(0.01, mid * rng.uniform(0.00005, 0.0004))
            out[symbol] = Quote(
                symbol=symbol,
                timestamp=now,
                bid_price=round(mid - half_spread, 2),
                bid_size=rng.uniform(1, 20) * 100,
                ask_price=round(mid + half_spread, 2),
                ask_size=rng.uniform(1, 20) * 100,
            )
        return out

    async def get_latest_trade(self, symbol: str) -> Trade | None:
        return Trade(
            symbol=symbol,
            timestamp=datetime.now(UTC),
            price=_base_price(symbol),
            size=100,
            exchange="SIM",
        )

    async def get_snapshots(self, symbols: list[str]) -> dict[str, Snapshot]:
        bars = await self.get_bars(symbols, timeframe="1Day", limit=3)
        quotes = await self.get_latest_quotes(symbols)
        out: dict[str, Snapshot] = {}
        for symbol in symbols:
            daily = bars.get(symbol, [])
            trade = await self.get_latest_trade(symbol)
            out[symbol] = Snapshot(
                symbol=symbol,
                latest_trade=trade,
                latest_quote=quotes.get(symbol),
                minute_bar=daily[-1] if daily else None,
                daily_bar=daily[-1] if daily else None,
                previous_daily_bar=daily[-2] if len(daily) > 1 else None,
            )
        return out


class SimulatedBroker(BrokerAdapter):
    """An in-memory paper broker.

    Fills market orders immediately at the simulated price. Limit and stop
    orders rest as `ACCEPTED` and are never filled: a fake fill engine would
    teach the operator wrong lessons, and the backtesting engine (Phase 2) is
    the right place for realistic fill modelling.
    """

    name = "simulated"

    def __init__(
        self,
        starting_equity: float = 100_000.0,
        market_data: SimulatedMarketData | None = None,
        market_open: bool = True,
    ) -> None:
        self._starting_equity = starting_equity
        self._cash = starting_equity
        self._data = market_data or SimulatedMarketData()
        self._orders: dict[str, Order] = {}
        self._by_client_id: dict[str, str] = {}
        self._positions: dict[str, Position] = {}
        self._market_open = market_open
        self._realized_pl = 0.0

    @property
    def is_paper(self) -> bool:
        return True

    def set_market_open(self, is_open: bool) -> None:
        """Test hook for the market-hours risk rule."""
        self._market_open = is_open

    # ------------------------------------------------------------------ #
    # account
    # ------------------------------------------------------------------ #

    async def get_account(self) -> AccountSnapshot:
        positions_value = sum(p.market_value for p in self._positions.values())
        equity = self._cash + positions_value
        return AccountSnapshot(
            account_id="SIMULATED-ACCOUNT",
            currency="USD",
            equity=round(equity, 2),
            last_equity=self._starting_equity,
            cash=round(self._cash, 2),
            # Cash account: buying power is just settled cash, no multiplier.
            buying_power=round(max(0.0, self._cash), 2),
            multiplier=1.0,
            portfolio_value=round(equity, 2),
            shorting_enabled=False,
            crypto_enabled=False,
            options_enabled=False,
        )

    async def get_capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker_name="simulated",
            detected=True,
            supports_crypto=False,
            supports_options=False,
            supports_direct_bonds=False,
            supports_fractional_shares=False,
            supports_short_selling=False,
            margin_enabled=False,
            max_leverage=1.0,
            data_feed="simulated",
            max_stream_symbols=9999,
            notes=[
                "SIMULATED broker. No credentials in use, no orders leave this process.",
                "Market orders fill instantly at the simulated price; resting "
                "orders are accepted but never filled.",
            ],
        )

    async def get_positions(self) -> list[Position]:
        # Mark to the current simulated price on every read, so unrealised P&L
        # moves the way a real position would.
        quotes = await self._data.get_latest_quotes(list(self._positions))
        for symbol, position in self._positions.items():
            quote = quotes.get(symbol)
            if quote:
                price = quote.mid
                position.current_price = price
                position.market_value = round(price * position.quantity, 2)
                position.unrealized_pl = round(
                    (price - position.average_entry_price) * position.quantity, 2
                )
                if position.cost_basis:
                    position.unrealized_pl_pct = round(
                        (position.unrealized_pl / abs(position.cost_basis)) * 100, 4
                    )
        return list(self._positions.values())

    async def get_orders(
        self, status: str = "open", limit: int = 50, symbols: list[str] | None = None
    ) -> list[Order]:
        orders = sorted(
            self._orders.values(),
            key=lambda o: o.submitted_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        if status == "open":
            orders = [o for o in orders if o.is_open]
        elif status == "closed":
            orders = [o for o in orders if not o.is_open]
        if symbols:
            orders = [o for o in orders if o.symbol in symbols]
        return orders[:limit]

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        order_id = self._by_client_id.get(client_order_id)
        return self._orders.get(order_id) if order_id else None

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #

    async def submit_order(self, request: OrderRequest) -> Order:
        # Mirror the real broker's duplicate-id behaviour. The Execution
        # Agent's idempotency logic is tested against this.
        if request.client_order_id in self._by_client_id:
            raise DuplicateOrder(f"client_order_id {request.client_order_id} already exists")

        if request.quantity <= 0:
            raise OrderRejected(f"quantity must be positive, got {request.quantity}")

        quote = await self._data.get_latest_quote(request.symbol)
        if quote is None:
            raise OrderRejected(f"no simulated market data for {request.symbol}")

        side = Side.SELL if request.side.lower() == "sell" else Side.BUY
        # Cross the spread, as a real market order would.
        fill_price = quote.ask_price if side is Side.BUY else quote.bid_price

        try:
            order_type = OrderType(request.order_type.lower())
        except ValueError:
            order_type = OrderType.MARKET
        try:
            tif = TimeInForce(request.time_in_force.lower())
        except ValueError:
            tif = TimeInForce.DAY

        order_id = uuid.uuid4().hex
        now = datetime.now(UTC)

        if order_type is OrderType.MARKET:
            notional = fill_price * request.quantity
            if side is Side.BUY and notional > self._cash:
                raise OrderRejected(
                    f"insufficient simulated buying power: need {notional:.2f}, "
                    f"have {self._cash:.2f}"
                )
            order = Order(
                id=order_id,
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                side=side,
                order_type=order_type,
                time_in_force=tif,
                status=OrderStatus.FILLED,
                quantity=request.quantity,
                filled_quantity=request.quantity,
                average_fill_price=fill_price,
                submitted_at=now,
                filled_at=now,
            )
            self._apply_fill(order)
        else:
            order = Order(
                id=order_id,
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                side=side,
                order_type=order_type,
                time_in_force=tif,
                status=OrderStatus.ACCEPTED,
                quantity=request.quantity,
                limit_price=request.limit_price,
                stop_price=request.stop_price,
                submitted_at=now,
            )

        self._orders[order_id] = order
        self._by_client_id[request.client_order_id] = order_id
        log.info(
            "simulated order accepted",
            extra={
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": order.quantity,
                "status": order.status.value,
                "client_order_id": order.client_order_id,
            },
        )
        return order

    def _apply_fill(self, order: Order) -> None:
        """Update cash and positions from a filled order."""
        price = order.average_fill_price or 0.0
        quantity = order.filled_quantity
        existing = self._positions.get(order.symbol)

        if order.side is Side.BUY:
            self._cash -= price * quantity
            if existing:
                total_quantity = existing.quantity + quantity
                existing.average_entry_price = round(
                    (existing.average_entry_price * existing.quantity + price * quantity)
                    / total_quantity,
                    4,
                )
                existing.quantity = total_quantity
                existing.cost_basis = round(existing.average_entry_price * total_quantity, 2)
            else:
                self._positions[order.symbol] = Position(
                    symbol=order.symbol,
                    quantity=quantity,
                    side=PositionSide.LONG,
                    average_entry_price=price,
                    current_price=price,
                    market_value=round(price * quantity, 2),
                    cost_basis=round(price * quantity, 2),
                )
        else:
            self._cash += price * quantity
            if existing:
                self._realized_pl += (price - existing.average_entry_price) * min(
                    quantity, existing.quantity
                )
                existing.quantity -= quantity
                if existing.quantity <= 1e-9:
                    self._positions.pop(order.symbol, None)
                else:
                    existing.cost_basis = round(existing.average_entry_price * existing.quantity, 2)
            # A sell with no position would be a short. This broker models a
            # cash account, so it books the cash but opens nothing: the Risk
            # Agent is responsible for never generating such an order.

    async def cancel_order(self, order_id: str) -> None:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderRejected(f"unknown order {order_id}")
        if not order.is_open:
            # Already terminal: cancelling is a no-op, not an error. Matches
            # how a real broker behaves on a race with a fill.
            return
        self._orders[order_id] = order.model_copy(
            update={"status": OrderStatus.CANCELED, "canceled_at": datetime.now(UTC)}
        )

    async def cancel_all_orders(self) -> int:
        count = 0
        for order_id, order in list(self._orders.items()):
            if order.is_open:
                self._orders[order_id] = order.model_copy(
                    update={
                        "status": OrderStatus.CANCELED,
                        "canceled_at": datetime.now(UTC),
                    }
                )
                count += 1
        return count

    async def close_position(self, symbol: str) -> Order | None:
        position = self._positions.get(symbol)
        if position is None:
            return None
        return await self.submit_order(
            OrderRequest(
                symbol=symbol,
                side="sell" if position.side is PositionSide.LONG else "buy",
                quantity=position.quantity,
                order_type="market",
                client_order_id=f"sim-flatten-{uuid.uuid4().hex[:10]}",
            )
        )

    async def close_all_positions(self, cancel_orders: bool = True) -> int:
        if cancel_orders:
            await self.cancel_all_orders()
        count = 0
        for symbol in list(self._positions):
            if await self.close_position(symbol):
                count += 1
        return count

    # ------------------------------------------------------------------ #
    # market session
    # ------------------------------------------------------------------ #

    async def get_clock(self) -> MarketClock:
        now = datetime.now(UTC)
        return MarketClock(
            timestamp=now,
            is_open=self._market_open,
            next_open=now + timedelta(hours=1),
            next_close=now + timedelta(hours=6),
        )

    async def get_asset_class(self, symbol: str) -> AssetClass:
        if "/" in symbol:
            # Consistent with get_capabilities(): crypto is not enabled here.
            return AssetClass.UNSUPPORTED
        return AssetClass.US_EQUITY
