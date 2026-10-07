"""Scanner scoring and ranking. Pure functions.

Separated from the agent so the ranking maths can be tested without market
data or a broker.

**How the score works.** Each enabled metric is normalised to 0-1 across the
candidates in *this* scan, then combined using the weights from
`scanner.yaml`. Normalising within the batch (rather than against absolute
thresholds) means the scanner answers "which of these is most interesting
right now", which is the question a ranked list should answer.

The consequence, worth being explicit about: a score is **relative**. A top
rank on a flat day means "least boring", not "good setup". The score breakdown
is attached to every result so the dashboard can show which metric earned the
rank, rather than presenting an unexplained number.
"""

from __future__ import annotations

from app.config.schema import ScannerConfig
from app.models.market import ScannerResult


def _normalise(values: list[float]) -> list[float]:
    """Min-max scale to 0-1. Identical values all become 0.5.

    Returning 0.5 rather than 0 or 1 for a degenerate spread keeps a metric
    that happens to be uniform from silently dominating or vanishing.
    """
    if not values:
        return []
    low = min(values)
    high = max(values)
    if high - low < 1e-12:
        return [0.5] * len(values)
    return [(v - low) / (high - low) for v in values]


def _metric_value(result: ScannerResult, metric: str) -> float | None:
    """Extract one metric, mapping booleans and signed values sensibly."""
    if metric == "ema_stack":
        return 1.0 if result.ema_stack_bullish else 0.0
    if metric == "distance_from_vwap_pct":
        # Above VWAP is the interesting condition for a long-biased scan, and
        # distance matters in both directions, so the absolute value would
        # reward a collapse equally. Clamp negatives to zero instead.
        value = result.distance_from_vwap_pct
        return max(0.0, value) if value is not None else None
    return getattr(result, metric, None)


def rank_candidates(results: list[ScannerResult], config: ScannerConfig) -> list[ScannerResult]:
    """Score, sort and rank. Returns a new list, highest score first."""
    if not results:
        return []

    weights = {k: v for k, v in config.ranking.weights.items() if v != 0}
    if not weights:
        # No weights configured: fall back to percent change so the scanner
        # still produces a defensible order rather than an arbitrary one.
        weights = {"percent_change": 1.0}

    total_weight = sum(abs(w) for w in weights.values()) or 1.0

    # Collect each metric across all candidates so it can be normalised.
    columns: dict[str, list[float | None]] = {
        metric: [_metric_value(r, metric) for r in results] for metric in weights
    }

    normalised: dict[str, list[float]] = {}
    for metric, raw in columns.items():
        present = [v for v in raw if v is not None]
        if not present:
            normalised[metric] = [0.0] * len(results)
            continue
        scaled = _normalise(present)
        # Re-expand to the full list, giving missing values a neutral 0.0 so a
        # symbol is never rewarded for having no data.
        iterator = iter(scaled)
        normalised[metric] = [0.0 if v is None else next(iterator) for v in raw]

    for index, result in enumerate(results):
        breakdown: dict[str, float] = {}
        score = 0.0
        for metric, weight in weights.items():
            contribution = normalised[metric][index] * weight
            breakdown[metric] = round(contribution, 4)
            score += contribution
        result.score = round(score / total_weight, 4)
        result.score_breakdown = breakdown

    ranked = sorted(results, key=lambda r: r.score, reverse=True)
    ranked = [r for r in ranked if r.score >= config.ranking.min_score]

    for position, result in enumerate(ranked, start=1):
        result.rank = position

    return ranked[: config.ranking.max_results]


def apply_filters(result: ScannerResult, config: ScannerConfig) -> list[str]:
    """Which filters this candidate fails. Empty means it passes.

    Failures are returned rather than raised so the scanner can report *why* a
    symbol was excluded — useful when a watchlist symbol keeps disappearing.
    """
    failed: list[str] = []
    filters = config.filters

    if result.price is None:
        failed.append("no_price")
        return failed

    if result.price < filters.min_price:
        failed.append(f"price_below_{filters.min_price}")
    if result.price > filters.max_price:
        failed.append(f"price_above_{filters.max_price}")

    if result.volume is not None and result.volume < filters.min_volume:
        failed.append(f"volume_below_{filters.min_volume}")

    if result.spread_percent is not None and result.spread_percent > filters.max_spread_percent:
        failed.append(f"spread_above_{filters.max_spread_percent}pct")

    return failed


def describe(result: ScannerResult) -> list[str]:
    """Short human-readable notes for the dashboard."""
    notes: list[str] = []

    if result.relative_volume is not None and result.relative_volume >= 1.5:
        notes.append(f"relative volume {result.relative_volume:.1f}x")
    if result.percent_change is not None and abs(result.percent_change) >= 1.0:
        notes.append(f"{result.percent_change:+.2f}% today")
    if result.ema_stack_bullish:
        notes.append("EMA stack bullish")
    if result.distance_from_vwap_pct is not None:
        side = "above" if result.distance_from_vwap_pct >= 0 else "below"
        notes.append(f"{abs(result.distance_from_vwap_pct):.2f}% {side} VWAP")
    if result.rsi is not None:
        if result.rsi >= 70:
            notes.append(f"RSI {result.rsi:.0f} overbought")
        elif result.rsi <= 30:
            notes.append(f"RSI {result.rsi:.0f} oversold")
    if result.atr_percent is not None and result.atr_percent >= 3.0:
        notes.append(f"ATR {result.atr_percent:.1f}% of price — wide ranges")

    return notes
