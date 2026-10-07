/**
 * COMMAND CENTER — the page you leave open.
 *
 * The layout follows the concept the operator asked for, with one change made
 * on purpose: the chart is the largest element, not a decoration beside a
 * table. A price chart is the thing you actually look at for hours, and the
 * previous version gave it a 70-pixel sparkline.
 *
 *   row 1   four headline numbers
 *   row 2   the chart, with an AI briefing and the movers beside it
 *   row 3   the market heatmap, and the scanner's ranking
 *   row 4   positions, and recent orders
 *   row 5   the Mentor's notes, and the live event stream
 *
 * Clicking a heatmap tile or a scanner row loads that symbol into the chart,
 * which is what makes the heatmap useful rather than ornamental.
 */

import { useState } from "react";
import { CandleChart } from "../components/CandleChart";
import { Heatmap, Movers } from "../components/Heatmap";
import { InsightPanel } from "../components/InsightPanel";
import {
  Badge,
  Empty,
  ErrorNote,
  MoneyStat,
  Panel,
  ScoreBar,
  Sparkline,
  Stat,
} from "../components/common";
import { api } from "../lib/api";
import { direction, money, percent, time } from "../lib/format";
import { usePoll } from "../lib/hooks";
import type { BusEvent, SystemStatus } from "../lib/types";

/** Bar sizes offered above the chart. */
const TIMEFRAMES = ["1Min", "5Min", "15Min", "1Hour", "1Day"] as const;

export function CommandCenter({
  status,
  events,
  onOpenAssistant,
}: {
  status: SystemStatus | null;
  events: BusEvent[];
  onOpenAssistant?: () => void;
}) {
  const [symbol, setSymbol] = useState("SPY");
  const [timeframe, setTimeframe] = useState<(typeof TIMEFRAMES)[number]>("5Min");

  // Short interval for the money, longer for the rest: the portfolio is what
  // the operator stares at, and everything else arrives over the socket.
  const portfolio = usePoll(api.portfolio, 5000);
  const watchlist = usePoll(api.watchlist, 15000);
  const scanner = usePoll(() => api.scannerResults(8), 15000);
  const orders = usePoll(() => api.orders("all", 8), 10000);
  const agents = usePoll(() => api.agents(0), 10000);
  const equity = usePoll(() => api.equityCurve(240), 60000);
  const explanations = usePoll(() => api.explanations(4), 20000);

  // `deps` is what makes the chart reload when the symbol or timeframe
  // changes; without it `usePoll` would keep calling the first closure.
  const bars = usePoll(
    () => api.bars(symbol, timeframe, 120),
    20000,
    [symbol, timeframe],
  );

  const book = portfolio.data?.portfolio;
  const equityPoints = (equity.data?.points ?? []).map((point) => point.equity);
  const rows = watchlist.data?.rows ?? [];

  // An agent that has never been started reports "created", not "stopped".
  // Checking only for "stopped" meant the "start it on the Agents page" hint
  // never appeared on a fresh install — exactly when it is most needed.
  const scannerIdle =
    scanner.data !== null &&
    !["idle", "working", "starting"].includes(scanner.data.agent_status);

  return (
    <>
      <Banners status={status} book={book} />

      {/* ---- row 1: headline numbers ----------------------------------- */}
      <div className="grid cols-4 tight">
        <MoneyStat
          label="Portfolio Value"
          value={book?.equity}
          subLabel={book ? `high water ${money(book.high_water_mark)}` : undefined}
        />
        <Stat
          label="Today's P&L"
          value={money(book?.daily_pl)}
          sub={book ? percent(book.daily_pl_pct) : undefined}
          tone={direction(book?.daily_pl)}
        />
        <MoneyStat label="Buying Power" value={book?.buying_power} subLabel="settled cash" />
        <Stat
          label="Open Positions"
          value={String(book?.open_position_count ?? 0)}
          sub={
            book
              ? `${percent(book.total_exposure_pct)} exposure · ${percent(-book.drawdown_pct)} drawdown`
              : undefined
          }
        />
      </div>

      <ErrorNote error={portfolio.error} />

      {/* ---- row 2: the chart, briefing, movers ------------------------ */}
      <div className="grid chart-row tight">
        <Panel
          title={`${symbol} — ${timeframe}`}
          actions={
            <div className="seg">
              {TIMEFRAMES.map((frame) => (
                <button
                  key={frame}
                  className={frame === timeframe ? "active" : ""}
                  onClick={() => setTimeframe(frame)}
                >
                  {frame}
                </button>
              ))}
            </div>
          }
        >
          {bars.error ? (
            <div className="error-text">{bars.error}</div>
          ) : (
            <CandleChart
              candles={bars.data?.bars ?? []}
              symbol={symbol}
              timeframe={timeframe}
              width={760}
              height={300}
            />
          )}
          <div className="faint mono chart-foot">
            {status?.broker.simulated
              ? "Simulated prices — no Alpaca credentials configured."
              : `Feed: ${status?.market_data.feed ?? "—"} · click a heatmap tile or a scanner row to change symbol`}
          </div>
        </Panel>

        <div className="stack">
          <InsightPanel onOpenAssistant={onOpenAssistant} />
          <Panel title="Movers" flush>
            <Movers rows={rows} limit={4} />
          </Panel>
        </div>
      </div>

      {/* ---- row 3: heatmap and scanner -------------------------------- */}
      <div className="grid cols-2 tight">
        <Panel
          title="Market Heatmap"
          actions={
            <span className="faint mono" style={{ fontSize: 10 }}>
              {rows.length} symbols · colour saturates at ±3%
            </span>
          }
        >
          <Heatmap rows={rows} onSelect={setSymbol} selected={symbol} />
        </Panel>

        <Panel
          title={`Top Opportunities — ${scanner.data?.universe ?? ""}`}
          flush
          actions={
            <span className="faint mono" style={{ fontSize: 10 }}>
              {scannerIdle
                ? "scanner not running"
                : `${scanner.data?.results.length ?? 0} ranked`}
            </span>
          }
        >
          {scanner.data && scanner.data.results.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Symbol</th>
                  <th className="num">Price</th>
                  <th className="num">Chg</th>
                  <th className="num">RVol</th>
                  <th>Score</th>
                </tr>
              </thead>
              <tbody>
                {scanner.data.results.map((result) => (
                  <tr
                    key={result.symbol}
                    className="clickable"
                    onClick={() => setSymbol(result.symbol)}
                    title={`Show ${result.symbol} on the chart`}
                  >
                    <td className="faint">{result.rank}</td>
                    <td className="symbol">{result.symbol}</td>
                    <td className="num">{money(result.price)}</td>
                    <td className={`num ${direction(result.percent_change)}`}>
                      {percent(result.percent_change)}
                    </td>
                    <td className="num">{result.relative_volume?.toFixed(2) ?? "—"}</td>
                    <td>
                      <ScoreBar score={result.score} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>
              {scannerIdle
                ? "The Market Scanner is not running. Start it on the Agents page and it will rank the watchlist."
                : "No candidates ranked yet. The scanner runs on its own interval."}
            </Empty>
          )}
        </Panel>
      </div>

      {/* ---- row 4: positions and orders ------------------------------- */}
      <div className="grid cols-2 tight">
        <Panel
          title="Positions"
          flush
          actions={
            <span className="faint mono" style={{ fontSize: 10 }}>
              {book ? `${money(book.unrealized_pl)} unrealised` : ""}
            </span>
          }
        >
          {book && book.positions.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th className="num">Qty</th>
                  <th className="num">Entry</th>
                  <th className="num">Last</th>
                  <th className="num">P&L</th>
                  <th className="num">%</th>
                </tr>
              </thead>
              <tbody>
                {book.positions.map((position) => (
                  <tr
                    key={position.symbol}
                    className="clickable"
                    onClick={() => setSymbol(position.symbol)}
                  >
                    <td className="symbol">{position.symbol}</td>
                    <td className="num">{position.quantity}</td>
                    <td className="num">{money(position.average_entry_price)}</td>
                    <td className="num">{money(position.current_price)}</td>
                    <td className={`num ${direction(position.unrealized_pl)}`}>
                      {money(position.unrealized_pl)}
                    </td>
                    <td className={`num ${direction(position.unrealized_pl_pct)}`}>
                      {percent(position.unrealized_pl_pct)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No open positions. The account is entirely in cash.</Empty>
          )}
        </Panel>

        <Panel title="Recent Orders" flush scroll>
          {orders.data && orders.data.orders.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th className="num">Qty</th>
                  <th className="num">Fill</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {orders.data.orders.map((order) => (
                  <tr key={order.id}>
                    <td className="faint">{time(order.submitted_at)}</td>
                    <td className="symbol">{order.symbol}</td>
                    <td>
                      <Badge tone={order.side}>{order.side}</Badge>
                    </td>
                    <td className="num">{order.quantity}</td>
                    <td className="num">{money(order.average_fill_price)}</td>
                    <td>
                      <Badge tone={order.status}>{order.status}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No orders yet. Nothing has traded.</Empty>
          )}
        </Panel>
      </div>

      {/* ---- row 5: equity, agents -------------------------------------
          `top` rather than the default stretch: the agent table is ten rows
          tall and the equity panel is one sparkline, so stretching left a
          400px box containing the words "not enough history yet". */}
      <div className="grid cols-2 tight top">
        <Panel
          title="Equity Curve"
          actions={
            <span className="faint mono" style={{ fontSize: 10 }}>
              {equityPoints.length} stored snapshots
            </span>
          }
        >
          <Sparkline values={equityPoints} width={520} height={78} />
          <div className="faint mono chart-foot">
            Snapshots are written every few minutes, so this fills in over time.
          </div>
        </Panel>

        <Panel title="Agent Activity" flush scroll>
          {agents.data ? (
            <table>
              <thead>
                <tr>
                  <th>Agent</th>
                  <th>Status</th>
                  <th>Doing</th>
                </tr>
              </thead>
              <tbody>
                {agents.data.agents.map((agent) => (
                  <tr key={agent.id}>
                    <td className="symbol">{agent.name}</td>
                    <td>
                      <Badge tone={agent.status}>{agent.status}</Badge>
                    </td>
                    <td className="faint" style={{ whiteSpace: "normal" }}>
                      {agent.current_task ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>Loading agents…</Empty>
          )}
        </Panel>
      </div>

      {/* ---- row 6: mentor and events ---------------------------------- */}
      <div className="grid cols-2">
        <Panel
          title="Mentor — What Just Happened"
          actions={
            <span className="faint mono" style={{ fontSize: 10 }}>
              deterministic teaching notes
            </span>
          }
          scroll
        >
          {explanations.data && explanations.data.explanations.length > 0 ? (
            explanations.data.explanations.map((note, index) => (
              <div className="note" key={`${note.timestamp}-${index}`}>
                <div className="row between">
                  <strong className="mono note-title">{note.title}</strong>
                  <span className="faint mono" style={{ fontSize: 10 }}>
                    {time(note.timestamp)}
                  </span>
                </div>
                <pre className="block">{note.body}</pre>
              </div>
            ))
          ) : (
            <Empty>
              Nothing to explain yet. The Mentor writes here when a signal
              fires, a trade is blocked, or an order fills.
            </Empty>
          )}
        </Panel>

        <Panel title="System Events" flush scroll>
          {events.length > 0 ? (
            <div className="logs" style={{ padding: "8px 12px" }}>
              {events.slice(0, 60).map((event) => (
                <div className="log-line" key={event.id}>
                  <span className="log-time">{time(event.timestamp)}</span>
                  <span className="log-logger">{event.topic}</span>
                  <span className="dim">{String(event.source ?? "")}</span>
                </div>
              ))}
            </div>
          ) : (
            <Empty>Waiting for events…</Empty>
          )}
        </Panel>
      </div>
    </>
  );
}

/** Warnings that need to be impossible to miss. */
function Banners({
  status,
  book,
}: {
  status: SystemStatus | null;
  book: { reconciliation_mismatch: boolean; mismatch_detail: string[] } | undefined;
}) {
  if (!status) return null;

  return (
    <>
      {status.kill_switch.engaged ? (
        <div className="banner danger">
          <strong>KILL SWITCH ENGAGED</strong>
          <span>
            {status.kill_switch.reason ?? "no reason recorded"}
            {status.kill_switch.triggered_by
              ? ` (by ${status.kill_switch.triggered_by})`
              : ""}
          </span>
          <span className="spacer" />
          <span className="faint">No new orders will be placed.</span>
        </div>
      ) : null}

      {book?.reconciliation_mismatch ? (
        <div className="banner warn">
          <strong>BROKER MISMATCH</strong>
          <span>
            ATLAS and the broker disagree about positions. The broker's numbers
            have been adopted; new orders are blocked until this clears.
          </span>
          <span className="faint">{book.mismatch_detail.slice(0, 2).join(" · ")}</span>
        </div>
      ) : null}

      {!status.broker.has_credentials ? (
        <div className="banner info">
          <strong>SIMULATED BROKER</strong>
          <span>
            No Alpaca credentials found, so prices and fills are synthetic. Add
            your paper keys to <code>trading/.env</code> and restart the backend
            to connect for real.
          </span>
        </div>
      ) : null}

      {status.startup_errors.length > 0 ? (
        <div className="banner warn">
          <strong>STARTUP WARNINGS</strong>
          <span>{status.startup_errors.join(" · ")}</span>
        </div>
      ) : null}

      {status.mode.warnings.length > 0 ? (
        <div className="banner info">
          <strong>CONFIG NOTES</strong>
          <span>{status.mode.warnings.join(" · ")}</span>
        </div>
      ) : null}
    </>
  );
}
