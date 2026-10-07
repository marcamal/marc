/** COMMAND CENTER — the page you leave open. */

import { api } from "../lib/api";
import { compact, direction, money, percent, time } from "../lib/format";
import { usePoll } from "../lib/hooks";
import type { BusEvent, SystemStatus } from "../lib/types";
import { Badge, Empty, ErrorNote, MoneyStat, Panel, ScoreBar, Sparkline, Stat } from "../components/common";

export function CommandCenter({
  status,
  events,
}: {
  status: SystemStatus | null;
  events: BusEvent[];
}) {
  // Short interval for the money, longer for the rest: the portfolio is what
  // the operator stares at, and everything else arrives over the socket.
  const portfolio = usePoll(api.portfolio, 5000);
  const watchlist = usePoll(api.watchlist, 15000);
  const scanner = usePoll(() => api.scannerResults(8), 15000);
  const orders = usePoll(() => api.orders("all", 10), 10000);
  const agents = usePoll(() => api.agents(0), 10000);
  const equity = usePoll(() => api.equityCurve(240), 60000);
  const explanations = usePoll(() => api.explanations(5), 20000);

  const book = portfolio.data?.portfolio;
  const equityPoints = (equity.data?.points ?? []).map((p) => p.equity);

  return (
    <>
      <Banners status={status} book={book} />

      {/* ---- headline numbers ------------------------------------------ */}
      <div className="grid cols-4" style={{ marginBottom: "var(--gap)" }}>
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

      {/* ---- main grid -------------------------------------------------- */}
      <div className="grid cols-2" style={{ marginBottom: "var(--gap)" }}>
        <Panel
          title="Equity Curve"
          actions={<span className="faint mono" style={{ fontSize: 10 }}>stored snapshots</span>}
        >
          <Sparkline values={equityPoints} width={520} height={70} />
          <div className="faint mono" style={{ fontSize: 10.5, marginTop: 6 }}>
            {equityPoints.length} points · snapshots are written every few minutes
          </div>
        </Panel>

        <Panel title="Positions" flush>
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
                {book.positions.map((p) => (
                  <tr key={p.symbol}>
                    <td className="symbol">{p.symbol}</td>
                    <td className="num">{p.quantity}</td>
                    <td className="num">{money(p.average_entry_price)}</td>
                    <td className="num">{money(p.current_price)}</td>
                    <td className={`num ${direction(p.unrealized_pl)}`}>
                      {money(p.unrealized_pl)}
                    </td>
                    <td className={`num ${direction(p.unrealized_pl_pct)}`}>
                      {percent(p.unrealized_pl_pct)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No open positions.</Empty>
          )}
        </Panel>
      </div>

      <div className="grid cols-2" style={{ marginBottom: "var(--gap)" }}>
        <Panel
          title={`Top Opportunities — ${scanner.data?.universe ?? ""}`}
          flush
          actions={
            <span className="faint mono" style={{ fontSize: 10 }}>
              {scanner.data?.agent_status === "stopped"
                ? "scanner stopped"
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
                {scanner.data.results.map((r) => (
                  <tr key={r.symbol}>
                    <td className="faint">{r.rank}</td>
                    <td className="symbol">{r.symbol}</td>
                    <td className="num">{money(r.price)}</td>
                    <td className={`num ${direction(r.percent_change)}`}>
                      {percent(r.percent_change)}
                    </td>
                    <td className="num">{r.relative_volume?.toFixed(2) ?? "—"}</td>
                    <td>
                      <ScoreBar score={r.score} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>
              {scanner.data?.agent_status === "stopped"
                ? "Scanner is stopped. Start it on the Agents page."
                : "No candidates yet."}
            </Empty>
          )}
        </Panel>

        <Panel title="Watchlist" flush scroll>
          {watchlist.data && watchlist.data.rows.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th className="num">Price</th>
                  <th className="num">Change</th>
                  <th className="num">Volume</th>
                </tr>
              </thead>
              <tbody>
                {watchlist.data.rows.map((row) => (
                  <tr key={row.symbol}>
                    <td className="symbol">{row.symbol}</td>
                    <td className="num">{money(row.price)}</td>
                    <td className={`num ${direction(row.percent_change)}`}>
                      {percent(row.percent_change)}
                    </td>
                    <td className="num faint">{compact(row.volume)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No watchlist data.</Empty>
          )}
        </Panel>
      </div>

      <div className="grid cols-2" style={{ marginBottom: "var(--gap)" }}>
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

      <div className="grid cols-2">
        <Panel
          title="Mentor — What Just Happened"
          actions={<span className="faint mono" style={{ fontSize: 10 }}>teaching notes</span>}
          scroll
        >
          {explanations.data && explanations.data.explanations.length > 0 ? (
            explanations.data.explanations.map((note, index) => (
              <div
                key={`${note.timestamp}-${index}`}
                style={{
                  marginBottom: 12,
                  paddingBottom: 10,
                  borderBottom: "1px solid var(--border)",
                }}
              >
                <div className="row between">
                  <strong className="mono" style={{ fontSize: 12 }}>
                    {note.title}
                  </strong>
                  <span className="faint mono" style={{ fontSize: 10 }}>
                    {time(note.timestamp)}
                  </span>
                </div>
                <pre className="block" style={{ marginTop: 6 }}>{note.body}</pre>
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
