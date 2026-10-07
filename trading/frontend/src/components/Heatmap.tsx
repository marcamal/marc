/**
 * The market heatmap: one tile per watchlist symbol, coloured by change.
 *
 * The colour scale is the whole design problem here. A linear green-to-red
 * ramp across the day's full range makes a quiet session look dramatic and a
 * volatile one look uniform, because the extremes set the scale. So the ramp
 * is anchored to a FIXED band of +/-3%, which is roughly a big day for a large
 * cap, and anything beyond it saturates. The consequence is that the same
 * colour always means the same move, across sessions and across symbols -
 * which is the only way a heatmap teaches you anything.
 *
 * Tiles are equal-sized rather than weighted by market cap. ATLAS does not
 * know market caps, and inventing a weighting would be a lie told in pixels.
 */

import { money, percent } from "../lib/format";

export interface HeatmapRow {
  symbol: string;
  price: number | null;
  percent_change: number | null;
}

/** Where the colour ramp saturates, in percent. */
const SATURATION = 3.0;

function tone(change: number | null | undefined): {
  background: string;
  color: string;
  border: string;
} {
  if (change === null || change === undefined || Number.isNaN(change)) {
    return {
      background: "var(--bg-raised)",
      color: "var(--text-faint)",
      border: "var(--border)",
    };
  }

  // 0 at flat, 1 at saturation. Square-rooted so small moves are still
  // visible: a linear ramp leaves everything under 0.5% looking identical.
  const magnitude = Math.min(1, Math.abs(change) / SATURATION);
  const intensity = Math.sqrt(magnitude);

  const hue = change >= 0 ? 142 : 0;
  const lightness = 12 + intensity * 26;
  const saturation = 20 + intensity * 55;

  return {
    background: `hsl(${hue} ${saturation}% ${lightness}%)`,
    // Keep the label readable at both ends of the ramp.
    color: intensity > 0.45 ? "#f4f8ff" : "var(--text-dim)",
    border: `hsl(${hue} ${saturation}% ${Math.min(60, lightness + 14)}%)`,
  };
}

export function Heatmap({
  rows,
  onSelect,
  selected,
}: {
  rows: HeatmapRow[];
  onSelect?: (symbol: string) => void;
  selected?: string;
}) {
  if (rows.length === 0) {
    return <div className="empty">No watchlist data yet.</div>;
  }

  // Strongest movers first, in both directions, so the interesting tiles are
  // top-left where the eye lands.
  const ordered = [...rows].sort(
    (a, b) => Math.abs(b.percent_change ?? 0) - Math.abs(a.percent_change ?? 0),
  );

  return (
    <div className="heatmap">
      {ordered.map((row) => {
        const colours = tone(row.percent_change);
        const isSelected = selected === row.symbol;
        return (
          <button
            key={row.symbol}
            type="button"
            className={`heat-tile${isSelected ? " selected" : ""}`}
            style={{
              background: colours.background,
              color: colours.color,
              borderColor: isSelected ? "var(--accent)" : colours.border,
            }}
            onClick={() => onSelect?.(row.symbol)}
            title={`${row.symbol} ${money(row.price)} ${percent(row.percent_change)}`}
          >
            <span className="heat-symbol">{row.symbol}</span>
            <span className="heat-change">{percent(row.percent_change, 1)}</span>
          </button>
        );
      })}
    </div>
  );
}

/**
 * The movers list: biggest gainers and losers, side by side.
 *
 * Separate from the heatmap on purpose. The heatmap answers "what does the
 * market look like"; this answers "what moved", which is a different question
 * and wants a number you can read rather than a colour you have to interpret.
 */
export function Movers({ rows, limit = 5 }: { rows: HeatmapRow[]; limit?: number }) {
  const withChange = rows.filter(
    (row) => row.percent_change !== null && !Number.isNaN(row.percent_change),
  );

  if (withChange.length === 0) {
    return <div className="empty">No price changes yet.</div>;
  }

  const sorted = [...withChange].sort(
    (a, b) => (b.percent_change ?? 0) - (a.percent_change ?? 0),
  );
  const gainers = sorted.slice(0, limit);
  // Taken from the other end and reversed so the worst is at the top of its
  // column, mirroring the gainers.
  const losers = sorted.slice(-limit).reverse();

  return (
    <div className="movers">
      <MoverColumn title="Gainers" rows={gainers} />
      <MoverColumn title="Losers" rows={losers} />
    </div>
  );
}

function MoverColumn({ title, rows }: { title: string; rows: HeatmapRow[] }) {
  return (
    <div className="mover-col">
      <div className="mover-head">{title}</div>
      {rows.map((row) => (
        <div className="mover-row" key={row.symbol}>
          <span className="symbol">{row.symbol}</span>
          <span className="spacer" />
          <span className="faint mono mover-price">{money(row.price)}</span>
          <span
            className={`mono mover-pct ${(row.percent_change ?? 0) >= 0 ? "up" : "down"}`}
          >
            {percent(row.percent_change, 1)}
          </span>
        </div>
      ))}
    </div>
  );
}
