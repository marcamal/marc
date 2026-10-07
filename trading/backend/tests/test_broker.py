"""Broker adapter tests.

The Alpaca adapter is tested against **mocked SDK objects**, never the network.
What matters is the translation layer: Alpaca returns many numeric fields as
strings and many datetimes as naive, and a mistake there silently corrupts
every downstream calculation.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.brokers.alpaca.market_data import is_crypto_symbol, parse_timeframe
from app.brokers.base import (
    BrokerAuthError,
    BrokerCapabilities,
    OrderRequest,
    normalise_order_status,
)
from app.brokers.factory import build_broker, build_market_data
from app.brokers.simulated import SimulatedBroker, SimulatedMarketData
from app.config.settings import Settings
from app.models.enums import AssetClass, OrderStatus, OrderType, PositionSide, Side

# --------------------------------------------------------------------------- #
# status normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("filled", OrderStatus.FILLED),
        ("FILLED", OrderStatus.FILLED),
        ("partially_filled", OrderStatus.PARTIALLY_FILLED),
        ("rejected", OrderStatus.REJECTED),
        (None, OrderStatus.UNKNOWN),
        ("", OrderStatus.UNKNOWN),
        ("some_future_alpaca_state", OrderStatus.UNKNOWN),
    ],
)
def test_order_status_normalisation(raw: str | None, expected: OrderStatus) -> None:
    """An unrecognised state must become UNKNOWN, never be guessed at.

    Mapping a new broker state onto FILLED would be catastrophic.
    """
    assert normalise_order_status(raw) is expected


def test_terminal_and_open_statuses() -> None:
    assert OrderStatus.FILLED.is_terminal is True
    assert OrderStatus.CANCELED.is_terminal is True
    assert OrderStatus.REJECTED.is_terminal is True
    assert OrderStatus.NEW.is_terminal is False
    assert OrderStatus.PARTIALLY_FILLED.is_open is True
    # UNKNOWN is treated as still open, so ATLAS keeps tracking it rather than
    # assuming it is finished.
    assert OrderStatus.UNKNOWN.is_open is True


# --------------------------------------------------------------------------- #
# timeframe parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "amount", "unit"),
    [
        ("1Min", 1, "Min"),
        ("5Min", 5, "Min"),
        ("15min", 15, "Min"),
        ("1Hour", 1, "Hour"),
        ("4Hour", 4, "Hour"),
        ("1Day", 1, "Day"),
        ("1Week", 1, "Week"),
        ("Day", 1, "Day"),
    ],
)
def test_parse_timeframe(value: str, amount: int, unit: str) -> None:
    timeframe = parse_timeframe(value)
    assert timeframe.amount_value == amount
    assert timeframe.unit_value == unit


@pytest.mark.parametrize("value", ["", "banana", "0Min", "-5Min", "1Fortnight"])
def test_parse_timeframe_rejects_nonsense(value: str) -> None:
    """A silently-defaulted timeframe would mean analysing the wrong chart."""
    with pytest.raises(ValueError):
        parse_timeframe(value)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [("BTC/USD", True), ("ETH/USD", True), ("SPY", False), ("BRK.B", False)],
)
def test_crypto_symbol_detection(symbol: str, expected: bool) -> None:
    assert is_crypto_symbol(symbol) is expected


# --------------------------------------------------------------------------- #
# Alpaca order conversion (mocked SDK objects)
# --------------------------------------------------------------------------- #


def test_order_conversion_from_sdk_shapes() -> None:
    """Alpaca sends numbers as strings and datetimes that may be naive."""
    from app.brokers.alpaca.broker import order_from_sdk

    raw = SimpleNamespace(
        id="abc-123",
        client_order_id="atlas-prop-0",
        symbol="NVDA",
        side=SimpleNamespace(value="buy"),
        order_type=SimpleNamespace(value="limit"),
        time_in_force=SimpleNamespace(value="day"),
        status=SimpleNamespace(value="partially_filled"),
        qty="10",  # string, as Alpaca sends it
        filled_qty="4",
        limit_price="178.50",
        stop_price=None,
        filled_avg_price="178.49",
        submitted_at=datetime(2026, 3, 2, 14, 30),  # naive, as the SDK may give
        filled_at=None,
        canceled_at=None,
        expired_at=None,
    )

    order = order_from_sdk(raw)

    assert order.id == "abc-123"
    assert order.symbol == "NVDA"
    assert order.side is Side.BUY
    assert order.order_type is OrderType.LIMIT
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.quantity == 10.0 and isinstance(order.quantity, float)
    assert order.filled_quantity == 4.0
    assert order.limit_price == 178.50
    assert order.remaining_quantity == 6.0
    assert order.is_open is True
    # The critical bit: a naive datetime must be made UTC-aware, or every
    # later comparison against an aware "now" raises.
    assert order.submitted_at is not None
    assert order.submitted_at.tzinfo is not None


def test_order_conversion_handles_missing_fields() -> None:
    from app.brokers.alpaca.broker import order_from_sdk

    order = order_from_sdk(SimpleNamespace(id="x", symbol="SPY"))

    assert order.id == "x"
    assert order.side is Side.BUY  # documented default
    assert order.status is OrderStatus.UNKNOWN
    assert order.quantity == 0.0
    assert order.limit_price is None


def test_alpaca_order_request_mapping() -> None:
    """Our OrderRequest must map onto the right SDK request class."""
    from app.brokers.alpaca.broker import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)  # no network, no credentials

    market = broker._build_alpaca_request(
        OrderRequest(symbol="SPY", side="buy", quantity=2, client_order_id="c1")
    )
    assert type(market).__name__ == "MarketOrderRequest"
    assert market.client_order_id == "c1"

    limit = broker._build_alpaca_request(
        OrderRequest(
            symbol="SPY",
            side="sell",
            quantity=2,
            order_type="limit",
            limit_price=600.0,
            client_order_id="c2",
        )
    )
    assert type(limit).__name__ == "LimitOrderRequest"
    assert limit.limit_price == 600.0

    stop = broker._build_alpaca_request(
        OrderRequest(
            symbol="SPY",
            side="sell",
            quantity=2,
            order_type="stop",
            stop_price=580.0,
            client_order_id="c3",
        )
    )
    assert type(stop).__name__ == "StopOrderRequest"
    assert stop.stop_price == 580.0


def test_alpaca_bracket_legs_attached() -> None:
    from app.brokers.alpaca.broker import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)
    request = broker._build_alpaca_request(
        OrderRequest(
            symbol="SPY",
            side="buy",
            quantity=1,
            client_order_id="c4",
            stop_loss_price=580.0,
            take_profit_price=600.0,
        )
    )
    assert request.stop_loss is not None
    assert request.stop_loss.stop_price == 580.0
    assert request.take_profit is not None
    assert request.take_profit.limit_price == 600.0


def test_alpaca_broker_requires_credentials() -> None:
    from app.brokers.alpaca.broker import AlpacaBroker

    with pytest.raises(BrokerAuthError, match="required"):
        AlpacaBroker(api_key="", secret_key="")


def _api_error(message: str, status_code: int) -> Any:
    """Build an Alpaca APIError with a given HTTP status.

    `APIError.status_code` is a read-only property that reads through to a
    wrapped HTTP error, so the status has to be supplied that way.
    """
    from alpaca.common.exceptions import APIError

    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status_code), request=None)
    return APIError(message, http_error)


def test_alpaca_auth_error_names_the_likely_cause() -> None:
    """The commonest failure is live keys in a paper config. Say so."""
    from app.brokers.alpaca.broker import AlpacaBroker

    translated = AlpacaBroker._translate(_api_error("forbidden", 403), "get_account")

    assert isinstance(translated, BrokerAuthError)
    assert "PAPER keys" in str(translated)


def test_alpaca_duplicate_order_detected() -> None:
    """A reused client_order_id must become DuplicateOrder, not a failure.

    This is what makes a retry after a network timeout safe.
    """
    from app.brokers.alpaca.broker import AlpacaBroker
    from app.brokers.base import DuplicateOrder

    error = _api_error("client_order_id must be unique, it already exists", 422)

    assert isinstance(AlpacaBroker._translate(error, "submit_order"), DuplicateOrder)


def test_alpaca_rate_limit_is_retryable() -> None:
    from app.brokers.alpaca.broker import AlpacaBroker
    from app.brokers.base import BrokerConnectionError

    translated = AlpacaBroker._translate(_api_error("too many requests", 429), "x")

    assert isinstance(translated, BrokerConnectionError)


def test_alpaca_bad_request_is_not_retryable() -> None:
    """A rejected order must not be resubmitted; it would be rejected again."""
    from app.brokers.alpaca.broker import AlpacaBroker
    from app.brokers.base import OrderRejected

    translated = AlpacaBroker._translate(_api_error("asset not tradable", 422), "submit")

    assert isinstance(translated, OrderRejected)


def test_network_failure_is_retryable() -> None:
    from app.brokers.alpaca.broker import AlpacaBroker
    from app.brokers.base import BrokerConnectionError

    translated = AlpacaBroker._translate(ConnectionError("reset by peer"), "submit")

    assert isinstance(translated, BrokerConnectionError)


# --------------------------------------------------------------------------- #
# the simulated broker
# --------------------------------------------------------------------------- #


async def test_simulated_account(broker: SimulatedBroker) -> None:
    account = await broker.get_account()
    assert account.equity == 100_000.0
    assert account.multiplier == 1.0, "should behave as a cash account"
    assert account.shorting_enabled is False


async def test_simulated_capabilities_are_conservative(broker: SimulatedBroker) -> None:
    capabilities = await broker.get_capabilities()
    assert capabilities.margin_enabled is False
    assert capabilities.supports_short_selling is False
    assert capabilities.supports_crypto is False
    assert "SIMULATED" in capabilities.notes[0]


async def test_simulated_market_order_fills_and_updates_cash(
    broker: SimulatedBroker,
) -> None:
    before = (await broker.get_account()).cash

    order = await broker.submit_order(
        OrderRequest(symbol="SPY", side="buy", quantity=2, client_order_id="c1")
    )

    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 2
    assert order.average_fill_price is not None

    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "SPY"
    assert positions[0].quantity == 2
    assert positions[0].side is PositionSide.LONG
    assert (await broker.get_account()).cash < before


async def test_simulated_market_order_crosses_the_spread(
    broker: SimulatedBroker, market_data: SimulatedMarketData
) -> None:
    """A buy should fill at the ask, as a real market order does."""
    quote = await market_data.get_latest_quote("SPY")
    assert quote is not None

    order = await broker.submit_order(
        OrderRequest(symbol="SPY", side="buy", quantity=1, client_order_id="c1")
    )
    assert order.average_fill_price == pytest.approx(quote.ask_price)


async def test_simulated_resting_orders_are_not_filled(broker: SimulatedBroker) -> None:
    """A fake fill engine would teach the wrong lessons.

    Limit orders rest as ACCEPTED; realistic fill modelling belongs in the
    backtesting engine.
    """
    order = await broker.submit_order(
        OrderRequest(
            symbol="SPY",
            side="buy",
            quantity=1,
            order_type="limit",
            limit_price=1.0,
            client_order_id="c1",
        )
    )
    assert order.status is OrderStatus.ACCEPTED
    assert order.filled_quantity == 0
    assert await broker.get_positions() == []


async def test_simulated_averages_the_entry_price_when_adding(
    broker: SimulatedBroker,
) -> None:
    await broker.submit_order(
        OrderRequest(symbol="SPY", side="buy", quantity=1, client_order_id="c1")
    )
    await broker.submit_order(
        OrderRequest(symbol="SPY", side="buy", quantity=1, client_order_id="c2")
    )

    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == 2


async def test_simulated_sell_closes_the_position(broker: SimulatedBroker) -> None:
    await broker.submit_order(
        OrderRequest(symbol="SPY", side="buy", quantity=2, client_order_id="c1")
    )
    await broker.submit_order(
        OrderRequest(symbol="SPY", side="sell", quantity=2, client_order_id="c2")
    )
    assert await broker.get_positions() == []


async def test_simulated_cancel_is_idempotent(broker: SimulatedBroker) -> None:
    """Cancelling an already-filled order races with a fill in real life."""
    order = await broker.submit_order(
        OrderRequest(symbol="SPY", side="buy", quantity=1, client_order_id="c1")
    )
    await broker.cancel_order(order.id)  # already FILLED, should be a no-op
    assert (await broker.get_orders(status="all"))[0].status is OrderStatus.FILLED


async def test_simulated_clock_and_asset_class(broker: SimulatedBroker) -> None:
    clock = await broker.get_clock()
    assert clock.is_open is True

    broker.set_market_open(False)
    assert (await broker.get_clock()).is_open is False

    assert await broker.get_asset_class("SPY") is AssetClass.US_EQUITY
    # Consistent with get_capabilities(), which reports no crypto support.
    assert await broker.get_asset_class("BTC/USD") is AssetClass.UNSUPPORTED


async def test_simulated_ping(broker: SimulatedBroker) -> None:
    assert await broker.ping() is True


# --------------------------------------------------------------------------- #
# the simulated market data
# --------------------------------------------------------------------------- #


async def test_simulated_bars_are_oldest_first(market_data: SimulatedMarketData) -> None:
    bars = (await market_data.get_bars(["SPY"], "5Min", limit=50))["SPY"]

    assert len(bars) == 50
    timestamps = [b.timestamp for b in bars]
    assert timestamps == sorted(timestamps), "every indicator expects oldest first"


async def test_simulated_bars_are_deterministic() -> None:
    """Reproducibility matters more than realism for a test fixture."""
    a = SimulatedMarketData(seed=99)
    b = SimulatedMarketData(seed=99)

    bars_a = (await a.get_bars(["NVDA"], "5Min", limit=30))["NVDA"]
    bars_b = (await b.get_bars(["NVDA"], "5Min", limit=30))["NVDA"]

    assert [x.close for x in bars_a] == [x.close for x in bars_b]


async def test_simulated_bars_differ_by_timeframe(
    market_data: SimulatedMarketData,
) -> None:
    """Without this, multi-timeframe analysis would be testing nothing."""
    five = (await market_data.get_bars(["SPY"], "5Min", limit=50))["SPY"]
    daily = (await market_data.get_bars(["SPY"], "1Day", limit=50))["SPY"]

    assert [b.close for b in five] != [b.close for b in daily]


async def test_simulated_prices_stay_near_the_seed(
    market_data: SimulatedMarketData,
) -> None:
    """The walk is anchored, so quotes do not drift into nonsense."""
    quote = await market_data.get_latest_quote("SPY")
    assert quote is not None
    assert 400 < quote.mid < 800, f"SPY simulated at {quote.mid}, which looks wrong"


async def test_simulated_volatility_is_realistic(
    market_data: SimulatedMarketData,
) -> None:
    """Per-bar moves must look like a market, not a crypto flash crash.

    Regression guard: the volatility scaling was once anchored at 5-minute
    bars and scaled *up* for longer ones, which produced roughly 20% daily
    candles — the dashboard showed SPY up 41% in a session. Volatility is now
    defined per day and scaled down.
    """
    import statistics

    five_min = (await market_data.get_bars(["SPY"], "5Min", limit=200))["SPY"]
    five_min_returns = [
        (five_min[i].close / five_min[i - 1].close - 1) * 100 for i in range(1, len(five_min))
    ]
    assert statistics.stdev(five_min_returns) < 0.5, (
        "a 5-minute bar moving more than 0.5% on average is not a real market"
    )

    daily = (await market_data.get_bars(["SPY"], "1Day", limit=120))["SPY"]
    daily_returns = [(daily[i].close / daily[i - 1].close - 1) * 100 for i in range(1, len(daily))]
    daily_stdev = statistics.stdev(daily_returns)
    assert 0.2 < daily_stdev < 4.0, f"daily volatility {daily_stdev:.2f}% is implausible"


async def test_simulated_daily_change_is_plausible(
    market_data: SimulatedMarketData,
) -> None:
    """The percent-change the dashboard shows must not be absurd."""
    snapshots = await market_data.get_snapshots(["SPY", "QQQ", "NVDA", "MSFT"])

    for symbol, snapshot in snapshots.items():
        change = snapshot.percent_change_today
        assert change is not None
        assert abs(change) < 10.0, f"{symbol} simulated at {change:+.2f}% today"


async def test_simulated_quotes_have_a_sane_spread(
    market_data: SimulatedMarketData,
) -> None:
    quote = await market_data.get_latest_quote("SPY")
    assert quote is not None
    assert quote.ask_price > quote.bid_price
    assert 0 < quote.spread_pct < 0.5
    assert quote.age_seconds < 5


async def test_simulated_bars_always_return_a_key_per_symbol(
    market_data: SimulatedMarketData,
) -> None:
    """Callers must never KeyError on a symbol that had no data."""
    bars = await market_data.get_bars(["SPY", "ZZZZ"], "5Min", limit=10)
    assert set(bars) == {"SPY", "ZZZZ"}


async def test_simulated_snapshots(market_data: SimulatedMarketData) -> None:
    snapshots = await market_data.get_snapshots(["SPY", "QQQ"])

    assert set(snapshots) == {"SPY", "QQQ"}
    spy = snapshots["SPY"]
    assert spy.price is not None
    assert spy.latest_quote is not None
    assert spy.percent_change_today is not None


async def test_bar_ohlc_is_internally_consistent(
    market_data: SimulatedMarketData,
) -> None:
    bars = (await market_data.get_bars(["SPY"], "5Min", limit=100))["SPY"]
    for bar in bars:
        assert bar.low <= bar.open <= bar.high
        assert bar.low <= bar.close <= bar.high
        assert bar.volume > 0


# --------------------------------------------------------------------------- #
# the factory
# --------------------------------------------------------------------------- #


def test_factory_returns_simulated_without_credentials() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert build_broker(settings).name == "simulated"
    assert build_market_data(settings).name == "simulated"


def test_factory_returns_simulated_for_backtest_mode() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ALPACA_API_KEY="k",
        ALPACA_SECRET_KEY="s",
        ATLAS_TRADING_MODE="backtest",
    )
    assert build_broker(settings).name == "simulated"


def test_factory_builds_alpaca_paper_with_credentials() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, ALPACA_API_KEY="PKTEST", ALPACA_SECRET_KEY="secret"
    )
    broker = build_broker(settings)

    assert broker.name == "alpaca"
    assert broker.is_paper is True, "must use the paper endpoint by default"  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# capability reporting
# --------------------------------------------------------------------------- #


def test_unsupported_summary_lists_what_is_missing() -> None:
    """The UI renders this as 'UNSUPPORTED BY CURRENT BROKER'."""
    capabilities = BrokerCapabilities(broker_name="test")
    missing = capabilities.unsupported_summary()

    assert "Crypto trading" in missing
    assert "Options trading" in missing
    assert any("bond ETFs" in m for m in missing), (
        "direct bonds should point the operator at bond ETFs instead"
    )


def test_enabled_features_are_not_listed_as_missing() -> None:
    capabilities = BrokerCapabilities(
        broker_name="test",
        supports_crypto=True,
        supports_options=True,
        supports_short_selling=True,
        supports_fractional_shares=True,
        margin_enabled=True,
    )
    missing = capabilities.unsupported_summary()

    assert "Crypto trading" not in missing
    assert "Options trading" not in missing
