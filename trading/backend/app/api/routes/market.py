"""Market data routes: clock, bars, quotes, snapshots."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import RuntimeDep
from app.brokers.base import BrokerError

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/clock")
async def get_clock(runtime: RuntimeDep) -> dict[str, Any]:
    """Exchange session state, from the broker.

    Never inferred from the local clock: holidays, half days and the
    operator's own timezone make that unreliable, and a wrong answer means
    trading into a closed market.
    """
    try:
        clock = await runtime.broker.get_clock()
    except BrokerError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    runtime.market_clock = clock
    return {
        "clock": clock.model_dump(mode="json"),
        "status": clock.status.value,
        "seconds_until_close": clock.seconds_until_close,
        "seconds_until_open": clock.seconds_until_open,
    }


@router.get("/bars/{symbol}")
async def get_bars(
    runtime: RuntimeDep,
    symbol: str,
    timeframe: str = Query(default="5Min"),
    limit: int = Query(default=100, ge=2, le=1000),
) -> dict[str, Any]:
    """Historical bars for one symbol, oldest first."""
    if runtime.data_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="market data service is not running",
        )
    try:
        bars = await runtime.data_service.get_bars(symbol.upper(), timeframe=timeframe, limit=limit)
    except (BrokerError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "count": len(bars),
        "bars": [b.model_dump(mode="json") for b in bars],
    }


@router.get("/quote/{symbol}")
async def get_quote(runtime: RuntimeDep, symbol: str) -> dict[str, Any]:
    """Latest bid/ask for one symbol, with its age.

    The age matters: the Risk Agent refuses to trade on a stale quote, so the
    dashboard shows the same number it judges on.
    """
    if runtime.data_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="market data service is not running",
        )
    try:
        quote = await runtime.data_service.get_quote(symbol.upper())
    except BrokerError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    if quote is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no quote available for {symbol.upper()}",
        )

    return {
        "quote": quote.model_dump(mode="json"),
        "age_seconds": round(quote.age_seconds, 2),
        "stale": quote.age_seconds > runtime.config.risk.max_data_staleness_seconds,
    }


@router.get("/indicators/{symbol}")
async def get_indicators(
    runtime: RuntimeDep,
    symbol: str,
    timeframe: str = Query(default="5Min"),
) -> dict[str, Any]:
    """Computed indicators for one symbol and timeframe."""
    if runtime.data_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="market data service is not running",
        )

    from app.market_data.indicators import compute_indicators

    bars = await runtime.data_service.get_bars(symbol.upper(), timeframe=timeframe, limit=200)
    if not bars:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no bars available for {symbol.upper()} on {timeframe}",
        )

    config = runtime.config.scanner.indicators
    indicators = compute_indicators(
        symbol=symbol.upper(),
        bars=bars,
        timeframe=timeframe,
        ema_fast=config.ema_fast,
        ema_slow=config.ema_slow,
        sma_long=config.sma_long,
        rsi_period=config.rsi_period,
        atr_period=config.atr_period,
        bollinger_period=config.bollinger_period,
        bollinger_std=config.bollinger_std,
        momentum_period=config.momentum_period,
    )
    return {
        "indicators": indicators.model_dump(mode="json"),
        "ema_stack_bullish": indicators.ema_stack_bullish,
        "above_vwap": indicators.above_vwap,
    }


@router.get("/snapshots")
async def get_snapshots(
    runtime: RuntimeDep,
    symbols: str = Query(description="Comma-separated symbols, e.g. SPY,QQQ,NVDA"),
) -> dict[str, Any]:
    """Batched snapshots: price, volume, previous close, quote."""
    if runtime.data_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="market data service is not running",
        )

    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()][:50]
    if not requested:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no symbols supplied")

    snapshots = await runtime.data_service.get_snapshots(requested)
    return {
        "snapshots": {
            symbol: {
                "price": snapshot.price,
                "percent_change_today": snapshot.percent_change_today,
                "quote": (
                    snapshot.latest_quote.model_dump(mode="json") if snapshot.latest_quote else None
                ),
                "daily_bar": (
                    snapshot.daily_bar.model_dump(mode="json") if snapshot.daily_bar else None
                ),
            }
            for symbol, snapshot in snapshots.items()
        }
    }


@router.get("/watchlist")
async def get_watchlist(runtime: RuntimeDep) -> dict[str, Any]:
    """The configured universe, with live prices.

    This is what the Command Center's watchlist panel renders.
    """
    symbols = runtime.config.scanner.active_symbols
    if runtime.data_service is None or not symbols:
        return {"universe": runtime.config.scanner.active, "symbols": symbols, "rows": []}

    snapshots = await runtime.data_service.get_snapshots(symbols)
    rows = []
    for symbol in symbols:
        snapshot = snapshots.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "price": snapshot.price if snapshot else None,
                "percent_change": snapshot.percent_change_today if snapshot else None,
                "volume": (snapshot.daily_bar.volume if snapshot and snapshot.daily_bar else None),
            }
        )
    return {
        "universe": runtime.config.scanner.active,
        "symbols": symbols,
        "rows": rows,
    }
