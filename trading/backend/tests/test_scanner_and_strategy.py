"""Scanner ranking and strategy pipeline tests."""

from __future__ import annotations

import pytest

from app.config.schema import AtlasConfig, ScannerConfig
from app.market_data.scanner import apply_filters, describe, rank_candidates
from app.models.market import ScannerResult
from app.runtime import AtlasRuntime
from app.strategies.base import StrategyContext
from app.strategies.ema_vwap_momentum import EmaVwapMomentumStrategy


def _result(symbol: str, **kwargs: object) -> ScannerResult:
    defaults: dict[str, object] = {"price": 100.0, "volume": 1_000_000.0}
    defaults.update(kwargs)
    return ScannerResult(symbol=symbol, **defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #


def test_ranking_orders_by_score(config: AtlasConfig) -> None:
    candidates = [
        _result("LOW", relative_volume=1.0, momentum_pct=0.1, percent_change=0.1),
        _result("HIGH", relative_volume=3.0, momentum_pct=5.0, percent_change=4.0),
        _result("MID", relative_volume=2.0, momentum_pct=2.0, percent_change=2.0),
    ]

    ranked = rank_candidates(candidates, config.scanner)

    assert [r.symbol for r in ranked] == ["HIGH", "MID", "LOW"]
    assert ranked[0].rank == 1
    assert ranked[0].score > ranked[-1].score


def test_ranking_attaches_a_breakdown(config: AtlasConfig) -> None:
    """A score must be explainable, not an unexplained number."""
    ranked = rank_candidates(
        [
            _result("A", relative_volume=3.0, momentum_pct=2.0),
            _result("B", relative_volume=1.0, momentum_pct=1.0),
        ],
        config.scanner,
    )

    assert ranked[0].score_breakdown
    assert set(ranked[0].score_breakdown) <= set(config.scanner.ranking.weights)


def test_ranking_handles_identical_candidates(config: AtlasConfig) -> None:
    """A degenerate spread must not make one metric dominate or vanish."""
    ranked = rank_candidates(
        [_result(s, relative_volume=2.0, momentum_pct=1.0) for s in ("A", "B", "C")],
        config.scanner,
    )

    assert len(ranked) == 3
    assert len({r.score for r in ranked}) == 1


def test_ranking_treats_missing_metrics_as_neutral(config: AtlasConfig) -> None:
    """A symbol must never be rewarded for having no data."""
    ranked = rank_candidates(
        [
            _result("HAS_DATA", relative_volume=3.0, momentum_pct=5.0),
            _result("NO_DATA"),
        ],
        config.scanner,
    )

    assert ranked[0].symbol == "HAS_DATA"


def test_ranking_respects_max_results(config: AtlasConfig) -> None:
    config.scanner.ranking.max_results = 3
    candidates = [_result(f"S{i}", relative_volume=float(i)) for i in range(10)]

    assert len(rank_candidates(candidates, config.scanner)) == 3


def test_ranking_of_empty_input(config: AtlasConfig) -> None:
    assert rank_candidates([], config.scanner) == []


def test_ranking_falls_back_when_no_weights_configured() -> None:
    """A defensible order beats an arbitrary one."""
    scanner = ScannerConfig(universes={"u": ["A"]}, active="u")
    scanner.ranking.weights = {}

    ranked = rank_candidates(
        [_result("A", percent_change=1.0), _result("B", percent_change=5.0)], scanner
    )

    assert ranked[0].symbol == "B"


def test_vwap_distance_does_not_reward_a_collapse(config: AtlasConfig) -> None:
    """Being far *below* VWAP is not bullish, so it must not score highly."""
    config.scanner.ranking.weights = {"distance_from_vwap_pct": 1.0}

    ranked = rank_candidates(
        [
            _result("ABOVE", distance_from_vwap_pct=2.0),
            _result("BELOW", distance_from_vwap_pct=-10.0),
        ],
        config.scanner,
    )

    assert ranked[0].symbol == "ABOVE"


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #


def test_filters_exclude_penny_stocks(config: AtlasConfig) -> None:
    failures = apply_filters(_result("CHEAP", price=0.5), config.scanner)
    assert any("price_below" in f for f in failures)


def test_filters_exclude_illiquid(config: AtlasConfig) -> None:
    failures = apply_filters(_result("THIN", volume=100.0), config.scanner)
    assert any("volume_below" in f for f in failures)


def test_filters_exclude_wide_spreads(config: AtlasConfig) -> None:
    failures = apply_filters(_result("WIDE", spread_percent=5.0), config.scanner)
    assert any("spread_above" in f for f in failures)


def test_filters_exclude_missing_price(config: AtlasConfig) -> None:
    assert apply_filters(_result("NOPRICE", price=None), config.scanner) == ["no_price"]


def test_good_candidate_passes_every_filter(config: AtlasConfig) -> None:
    good = _result("SPY", price=585.0, volume=50_000_000.0, spread_percent=0.01)
    assert apply_filters(good, config.scanner) == []


def test_describe_produces_readable_notes() -> None:
    notes = describe(
        _result(
            "NVDA",
            relative_volume=2.3,
            percent_change=3.4,
            ema_stack_bullish=True,
            distance_from_vwap_pct=1.2,
            rsi=75.0,
            atr_percent=4.0,
        )
    )

    joined = " | ".join(notes)
    assert "relative volume 2.3x" in joined
    assert "EMA stack bullish" in joined
    assert "overbought" in joined


# --------------------------------------------------------------------------- #
# the scanner agent
# --------------------------------------------------------------------------- #


async def test_scanner_produces_ranked_results(runtime: AtlasRuntime) -> None:
    await runtime.agents.start_agent("scanner")
    agent = runtime.agents.get("scanner")

    await agent.run_cycle()  # type: ignore[union-attr]

    results = agent.results  # type: ignore[union-attr]
    assert results, "the scanner should have produced candidates"
    assert results[0].rank == 1
    assert all(r.score >= 0 for r in results)
    # Ranks must be contiguous from 1.
    assert [r.rank for r in results] == list(range(1, len(results) + 1))


async def test_scanner_publishes_an_event(runtime: AtlasRuntime) -> None:
    import asyncio

    from app.events.types import ScannerResultsEvent, Topics

    received: list[ScannerResultsEvent] = []

    async def handler(event: object) -> None:
        received.append(event)  # type: ignore[arg-type]

    runtime.bus.subscribe(Topics.SCANNER_RESULTS, handler, name="t")
    await runtime.agents.start_agent("scanner")
    await runtime.agents.get("scanner").run_cycle()  # type: ignore[union-attr]
    await asyncio.sleep(0.1)

    assert received
    assert received[0].universe == runtime.config.scanner.active


async def test_scanner_detail_payload(runtime: AtlasRuntime) -> None:
    await runtime.agents.start_agent("scanner")
    agent = runtime.agents.get("scanner")
    await agent.run_cycle()  # type: ignore[union-attr]

    detail = agent.detail()  # type: ignore[union-attr]
    assert detail["universe"] == runtime.config.scanner.active
    assert detail["ranked"] > 0
    assert "top" in detail


# --------------------------------------------------------------------------- #
# strategy gating
# --------------------------------------------------------------------------- #


def test_strategies_are_disabled_by_default(runtime: AtlasRuntime) -> None:
    """Nothing may trade until the operator explicitly turns it on."""
    assert runtime.strategies.globally_enabled is False
    assert runtime.strategies.active == []


def test_strategy_loads_from_config(runtime: AtlasRuntime) -> None:
    strategy = runtime.strategies.get("ema_vwap_momentum_v1")

    assert strategy is not None
    assert isinstance(strategy, EmaVwapMomentumStrategy)
    assert strategy.enabled is False


def test_enabling_one_strategy_is_not_enough(runtime: AtlasRuntime) -> None:
    """The global switch is the master brake and lives only in the file."""
    strategy = runtime.strategies.get("ema_vwap_momentum_v1")
    assert strategy is not None

    strategy.enable()

    assert strategy.enabled is True
    assert runtime.strategies.active == [], "the global switch must still block it"


def test_both_gates_open_activates_the_strategy(runtime: AtlasRuntime) -> None:
    runtime.strategies._config.strategies_globally_enabled = True
    strategy = runtime.strategies.get("ema_vwap_momentum_v1")
    assert strategy is not None

    strategy.enable()

    assert [s.id for s in runtime.strategies.active] == ["ema_vwap_momentum_v1"]


# --------------------------------------------------------------------------- #
# the example strategy
# --------------------------------------------------------------------------- #


def _context(
    runtime_account: object, bars: dict, indicators: dict, **kwargs: object
) -> StrategyContext:
    return StrategyContext(
        account=runtime_account,  # type: ignore[arg-type]
        positions=[],
        bars=bars,
        indicators=indicators,
        market_open=bool(kwargs.get("market_open", True)),
        seconds_until_close=kwargs.get("seconds_until_close", 7200),  # type: ignore[arg-type]
    )


def _strategy(runtime: AtlasRuntime) -> EmaVwapMomentumStrategy:
    strategy = runtime.strategies.get("ema_vwap_momentum_v1")
    assert isinstance(strategy, EmaVwapMomentumStrategy)
    return strategy


async def test_strategy_produces_nothing_when_market_closed(
    runtime: AtlasRuntime, account: object
) -> None:
    strategy = _strategy(runtime)
    context = _context(account, {}, {}, market_open=False)

    assert strategy.generate(context) == []


async def test_strategy_produces_nothing_near_the_close(
    runtime: AtlasRuntime, account: object
) -> None:
    """No new intraday position with no time for the thesis to play out."""
    strategy = _strategy(runtime)
    context = _context(account, {}, {}, seconds_until_close=60)

    assert strategy.generate(context) == []


async def test_strategy_requires_every_condition(runtime: AtlasRuntime, account: object) -> None:
    """A bearish setup must produce no proposal."""
    from app.market_data.indicators import compute_indicators
    from tests.conftest import make_bars

    strategy = _strategy(runtime)
    downtrend = make_bars(symbol="SPY", count=120, start_price=200.0, trend=-0.5)
    indicators = {"SPY": compute_indicators("SPY", downtrend)}

    assert strategy.generate(_context(account, {"SPY": downtrend}, indicators)) == []


async def test_strategy_proposal_is_internally_coherent(
    runtime: AtlasRuntime, account: object
) -> None:
    """When it does fire, the proposal must be a valid, risk-sized trade."""
    from app.market_data.indicators import compute_indicators
    from tests.conftest import make_bars

    strategy = _strategy(runtime)
    strategy.symbols = ["SPY"]
    # A rising series with pullbacks (so RSI stays under the overbought
    # ceiling) and a volume surge on the last bar (so relative volume clears
    # the threshold). A perfectly linear rise would give RSI 100 and be
    # correctly rejected by the strategy's own filter.
    bars = make_bars(symbol="SPY", count=120, start_price=100.0, trend=0.3, wobble=0.8)
    bars[-1] = bars[-1].model_copy(update={"volume": 5_000_000.0})
    indicators = compute_indicators("SPY", bars)

    # Assert the fixture really does present a valid setup, so that a failure
    # below points at the strategy rather than at the test data.
    assert indicators.ema_stack_bullish is True
    assert indicators.above_vwap is True
    assert indicators.rsi is not None and indicators.rsi < strategy.rsi_max
    assert indicators.relative_volume is not None
    assert indicators.relative_volume >= strategy.min_relative_volume

    proposals = strategy.generate(_context(account, {"SPY": bars}, {"SPY": indicators}))

    assert proposals, "a textbook setup should have produced a proposal"
    proposal = proposals[0]
    assert proposal.symbol == "SPY"
    assert proposal.side.value == "buy"
    assert proposal.stop_price < proposal.entry_price, "a long needs a stop below entry"
    assert proposal.target_price is not None
    assert proposal.target_price > proposal.entry_price
    assert proposal.quantity >= 1
    assert proposal.quantity == int(proposal.quantity), "no fractional shares"
    assert proposal.invalidation, "every proposal must state what would disprove it"
    assert proposal.evidence, "every proposal must carry its reasoning"
    assert proposal.expires_at is not None, "a price-based setup must expire"

    # Position size must respect the strategy's own risk budget.
    risk_pct = (proposal.estimated_risk / account.equity) * 100  # type: ignore[attr-defined]
    assert risk_pct <= strategy.max_risk_pct + 0.01


def test_position_sizing_formula() -> None:
    """The canonical formula, checked against a hand calculation."""
    from app.risk.rules import size_position

    # 1% of 100,000 = 1,000 risk budget; 5.00 per share => 200 shares.
    assert size_position(100_000, 100.0, 95.0, 1.0) == 200.0
    # 0.5% of 10,000 = 50; 2.00 per share => 25 shares.
    assert size_position(10_000, 50.0, 48.0, 0.5) == 25.0


def test_position_sizing_rounds_down() -> None:
    """Rounding up would breach the very limit being respected."""
    from app.risk.rules import size_position

    quantity = size_position(10_000, 100.0, 97.0, 1.0)  # 100/3 = 33.33
    assert quantity == 33.0


def test_position_sizing_degenerate_inputs() -> None:
    from app.risk.rules import size_position

    assert size_position(0, 100.0, 95.0, 1.0) == 0.0
    assert size_position(100_000, 100.0, 100.0, 1.0) == 0.0  # zero stop distance
    assert size_position(100_000, 0.0, 95.0, 1.0) == 0.0


def test_position_sizing_allows_fractional_when_permitted() -> None:
    from app.risk.rules import size_position

    quantity = size_position(10_000, 100.0, 97.0, 1.0, allow_fractional=True)
    assert quantity == pytest.approx(33.3333, abs=0.001)


# --------------------------------------------------------------------------- #
# the strategy agent
# --------------------------------------------------------------------------- #


async def test_strategy_agent_skips_when_globally_disabled(
    runtime: AtlasRuntime,
) -> None:
    await runtime.agents.start_agent("strategy")
    agent = runtime.agents.get("strategy")

    await agent.run_cycle()  # type: ignore[union-attr]

    assert agent.proposals_published == 0  # type: ignore[union-attr]
    assert "globally_enabled is false" in (agent.skip_reason or "")  # type: ignore[union-attr]


async def test_strategy_agent_skips_when_kill_switch_engaged(
    runtime: AtlasRuntime,
) -> None:
    runtime.strategies._config.strategies_globally_enabled = True
    strategy = runtime.strategies.get("ema_vwap_momentum_v1")
    assert strategy is not None
    strategy.enable()
    await runtime.engage_kill_switch("test")

    await runtime.agents.start_agent("strategy")
    agent = runtime.agents.get("strategy")
    await agent.run_cycle()  # type: ignore[union-attr]

    assert agent.proposals_published == 0  # type: ignore[union-attr]
    assert "kill switch" in (agent.skip_reason or "")  # type: ignore[union-attr]


async def test_strategy_agent_survives_a_raising_strategy(
    runtime: AtlasRuntime,
) -> None:
    """One broken strategy must not stop the agent or the others."""
    runtime.strategies._config.strategies_globally_enabled = True
    strategy = runtime.strategies.get("ema_vwap_momentum_v1")
    assert strategy is not None
    strategy.enable()

    def exploding(_context: object) -> list:
        raise RuntimeError("strategy bug")

    strategy.generate = exploding  # type: ignore[assignment]

    await runtime.agents.start_agent("strategy")
    agent = runtime.agents.get("strategy")
    await agent.run_cycle()  # type: ignore[union-attr]

    assert strategy.last_error is not None
    assert agent.status.is_running  # type: ignore[union-attr]
