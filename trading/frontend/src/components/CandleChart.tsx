/**
 * A candlestick chart in plain SVG.
 *
 * Why not a charting library: the two obvious candidates add 150-400KB to a
 * dashboard whose whole point is that it starts instantly on a local machine,
 * and both want to own layout and theming. A candle is a rectangle and two
 * lines. The interesting work here is not the drawing, it is the parts a
 * library would not get right for this use:
 *
 *   - the price axis is padded by a fraction of the range, not snapped to
 *     zero, because a 0.4% intraday move on a $585 index must still be legible
 *   - volume shares the frame instead of taking a second axis, scaled to the
 *     bottom fifth, so the price panel keeps its height
 *   - the last close draws a dashed line across the full width, which is the
 *     one reference you actually look for
 *   - hovering reads out the real OHLCV of that bar rather than interpolating
 *
 * Deliberately no indicators drawn here. An EMA on a chart that disagreed with
 * the EMA the Technical Agent computed would be worse than no EMA at all, and
 * the agents own that maths.
 */

import { useMemo, useState } from "react";
import { compact, money, percent } from "../lib/format";

export interface Candle {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

const PAD = { top: 10, right: 54, bottom: 18, left: 8 };
/** Share of the frame given to the volume histogram. */
const VOLUME_SHARE = 0.18;
/** Padding added above and below the price range, as a share of the range. */
const RANGE_PAD = 0.08;

export function CandleChart({
  candles,
  width = 680,
  height = 260,
  symbol,
  timeframe,
}: {
  candles: Candle[];
  width?: number;
  height?: number;
  symbol?: string;
  timeframe?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const layout = useMemo(() => {
    if (candles.length < 2) return null;

    const plotWidth = width - PAD.left - PAD.right;
    const plotHeight = height - PAD.top - PAD.bottom;
    const priceHeight = plotHeight * (1 - VOLUME_SHARE);
    const volumeHeight = plotHeight * VOLUME_SHARE;

    let low = Infinity;
    let high = -Infinity;
    let maxVolume = 0;
    for (const candle of candles) {
      if (candle.low < low) low = candle.low;
      if (candle.high > high) high = candle.high;
      if (candle.volume > maxVolume) maxVolume = candle.volume;
    }

    // Pad the range so a small move still fills the frame. Without this a
    // quiet session draws a flat line across the middle.
    const span = high - low || high * 0.01 || 1;
    const padded = span * RANGE_PAD;
    low -= padded;
    high += padded;

    const step = plotWidth / candles.length;
    // Leave a gap between candles, but never let the body vanish.
    const bodyWidth = Math.max(1, Math.min(14, step * 0.68));

    const y = (price: number) =>
      PAD.top + priceHeight - ((price - low) / (high - low)) * priceHeight;
    const x = (index: number) => PAD.left + index * step + step / 2;

    return {
      low,
      high,
      maxVolume,
      step,
      bodyWidth,
      priceHeight,
      volumeHeight,
      plotWidth,
      x,
      y,
    };
  }, [candles, width, height]);

  if (!layout) {
    return (
      <div className="chart-empty">
        {candles.length === 0 ? "No price history yet." : "Not enough bars to draw a chart."}
      </div>
    );
  }

  const { low, high, maxVolume, step, bodyWidth, priceHeight, volumeHeight, x, y } = layout;

  const first = candles[0]!;
  const last = candles[candles.length - 1]!;
  // The move across the bars on screen, not the day's change. The header
  // labels it as such; see the note there.
  const windowChange = ((last.close - first.open) / first.open) * 100;
  const rising = last.close >= first.open;

  const shown = hover !== null ? candles[hover] : null;

  // Four horizontal guides, labelled on the right.
  const gridLines = [0, 0.25, 0.5, 0.75, 1].map((fraction) => {
    const price = low + (high - low) * (1 - fraction);
    return { price, y: PAD.top + priceHeight * fraction };
  });

  return (
    <div className="chart">
      <div className="chart-head">
        <span className="chart-symbol">{symbol ?? ""}</span>
        <span className={`chart-price ${rising ? "up" : "down"}`}>{money(last.close)}</span>
        <span className={`chart-change ${rising ? "up" : "down"}`}>
          {percent(windowChange)}
        </span>
        {/* Labelled explicitly, because this is NOT the day's change. It is
            the move across the bars on screen, which at 120 x 5Min is about
            ten hours and at 120 x 1Day is six months. Showing it unlabelled
            next to the heatmap's daily percentages invited exactly the wrong
            comparison. */}
        <span className="chart-window faint mono">
          over {candles.length} x {timeframe ?? "bars"}
        </span>
        <span className="spacer" />
        {shown ? (
          <span className="chart-readout mono">
            O {shown.open.toFixed(2)} H {shown.high.toFixed(2)} L {shown.low.toFixed(2)} C{" "}
            {shown.close.toFixed(2)} V {compact(shown.volume)}
          </span>
        ) : (
          <span className="chart-readout mono faint">hover a candle for OHLCV</span>
        )}
      </div>

      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${symbol ?? "price"} candlestick chart`}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id="candle-glow" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.1" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </linearGradient>
        </defs>

        {gridLines.map((line) => (
          <g key={line.y}>
            <line
              x1={PAD.left}
              x2={width - PAD.right}
              y1={line.y}
              y2={line.y}
              className="chart-grid"
            />
            <text x={width - PAD.right + 6} y={line.y + 3.5} className="chart-axis">
              {line.price.toFixed(2)}
            </text>
          </g>
        ))}

        {/* Volume, along the bottom. */}
        {candles.map((candle, index) => {
          const barHeight = maxVolume > 0 ? (candle.volume / maxVolume) * volumeHeight : 0;
          return (
            <rect
              key={`v${candle.timestamp}-${index}`}
              x={x(index) - bodyWidth / 2}
              y={height - PAD.bottom - barHeight}
              width={bodyWidth}
              height={Math.max(0.5, barHeight)}
              className={candle.close >= candle.open ? "vol up" : "vol down"}
            />
          );
        })}

        {/* The last close, as the reference you actually look for. */}
        <line
          x1={PAD.left}
          x2={width - PAD.right}
          y1={y(last.close)}
          y2={y(last.close)}
          className={`chart-last ${rising ? "up" : "down"}`}
        />
        <rect
          x={width - PAD.right + 2}
          y={y(last.close) - 8}
          width={PAD.right - 4}
          height={16}
          rx={2}
          className={`chart-last-tag ${rising ? "up" : "down"}`}
        />
        <text
          x={width - PAD.right + 6}
          y={y(last.close) + 3.5}
          className="chart-last-label"
        >
          {last.close.toFixed(2)}
        </text>

        {/* Candles. */}
        {candles.map((candle, index) => {
          const up = candle.close >= candle.open;
          const bodyTop = y(Math.max(candle.open, candle.close));
          const bodyBottom = y(Math.min(candle.open, candle.close));
          return (
            <g
              key={`${candle.timestamp}-${index}`}
              className={`candle ${up ? "up" : "down"}${hover === index ? " active" : ""}`}
            >
              <line x1={x(index)} x2={x(index)} y1={y(candle.high)} y2={y(candle.low)} />
              <rect
                x={x(index) - bodyWidth / 2}
                y={bodyTop}
                width={bodyWidth}
                // A doji would otherwise be invisible, so floor the height.
                height={Math.max(1, bodyBottom - bodyTop)}
              />
            </g>
          );
        })}

        {/* Hover targets, on top and invisible. One per bar, full height, so
            the pointer does not have to find a thin candle. */}
        {candles.map((candle, index) => (
          <rect
            key={`h${candle.timestamp}-${index}`}
            x={PAD.left + index * step}
            y={0}
            width={step}
            height={height}
            fill="transparent"
            onMouseEnter={() => setHover(index)}
          />
        ))}

        {hover !== null ? (
          <line
            x1={x(hover)}
            x2={x(hover)}
            y1={PAD.top}
            y2={height - PAD.bottom}
            className="chart-crosshair"
          />
        ) : null}
      </svg>
    </div>
  );
}
