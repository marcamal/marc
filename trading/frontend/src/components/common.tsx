/** Small shared presentational components. */

import type { ReactNode } from "react";
import { direction, money, percent } from "../lib/format";

export function Panel({
  title,
  actions,
  children,
  flush,
  scroll,
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  scroll?: boolean;
}) {
  return (
    <section className="panel">
      <header className="panel-head">
        <span className="panel-title">{title}</span>
        <span className="spacer" />
        {actions}
      </header>
      <div
        className={`panel-body${flush ? " flush" : ""}${scroll ? " scroll" : ""}`}
      >
        {children}
      </div>
    </section>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "up" | "down" | "flat";
}) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value${tone ? ` ${tone}` : ""}`}>{value}</div>
      {sub ? <div className="stat-sub">{sub}</div> : null}
    </div>
  );
}

/** A money figure with a signed percentage underneath, coloured by direction. */
export function MoneyStat({
  label,
  value,
  changePct,
  subLabel,
}: {
  label: string;
  value: number | null | undefined;
  changePct?: number | null;
  subLabel?: string;
}) {
  const tone = changePct === undefined ? undefined : direction(changePct);
  const sub =
    changePct === undefined || changePct === null
      ? subLabel
      : `${percent(changePct)}${subLabel ? ` · ${subLabel}` : ""}`;
  return <Stat label={label} value={money(value)} sub={sub} tone={tone} />;
}

export function Dot({ state }: { state: "ok" | "active" | "warn" | "error" | "off" }) {
  return <span className={`dot ${state}`} />;
}

export function Indicator({
  label,
  state,
  title,
}: {
  label: string;
  state: "ok" | "active" | "warn" | "error" | "off";
  title?: string;
}) {
  return (
    <span className="indicator" title={title}>
      <Dot state={state} />
      {label}
    </span>
  );
}

export function Badge({
  children,
  tone,
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`badge${tone ? ` ${tone}` : ""}`}>{children}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="error-text">{error}</div>;
}

/** A minimal inline-SVG sparkline. No charting dependency for one line. */
export function Sparkline({
  values,
  width = 220,
  height = 44,
}: {
  values: number[];
  width?: number;
  height?: number;
}) {
  if (values.length < 2) {
    return <div className="faint mono" style={{ fontSize: 11 }}>not enough history yet</div>;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);

  const points = values
    .map((value, index) => {
      const x = index * step;
      // SVG y grows downward, so invert.
      const y = height - ((value - min) / span) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const first = values[0] ?? 0;
  const last = values[values.length - 1] ?? 0;
  const stroke = last >= first ? "var(--up)" : "var(--down)";

  return (
    <svg width={width} height={height} role="img" aria-label="equity curve">
      <polyline
        points={points}
        fill="none"
        stroke={stroke}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** A 0..1 score rendered as a bar plus the number. */
export function ScoreBar({ score }: { score: number }) {
  const pct = Math.max(0, Math.min(1, score)) * 100;
  return (
    <span className="row" style={{ gap: 8 }}>
      <span className="bar">
        <span style={{ width: `${pct}%` }} />
      </span>
      <span className="faint">{score.toFixed(3)}</span>
    </span>
  );
}
