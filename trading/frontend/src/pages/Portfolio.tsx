/** PORTFOLIO — positions, allocation, exposure and order history. */

import { api } from "../lib/api";
import { direction, money, percent, time } from "../lib/format";
import { usePoll } from "../lib/hooks";
import { Badge, Empty, ErrorNote, MoneyStat, Panel, Sparkline, Stat } from "../components/common";

export function Portfolio() {
  const portfolio = usePoll(api.portfolio, 5000);
  const orders = usePoll(() => api.orders("all", 60), 10000);
  const equity = usePoll(() => api.equityCurve(500), 30000);

  const book = portfolio.data?.portfolio;
  const points = (equity.data?.points ?? []).map((p) => p.equity);

  const groups = Object.entries(book?.exposure_by_correlation_group ?? {}).sort(
    (a, b) => b[1] - a[1],
  );

  return (
    <>
      <div className="grid cols-4" style={{ marginBottom: "var(--gap)" }}>
        <MoneyStat label="Equity" value={book?.equity} />
        <Stat
          label="Unrealised P&L"
          value={money(book?.unrealized_pl)}
          tone={direction(book?.unrealized_pl)}
        />
        <Stat
          label="Exposure"
          value={percent(book?.total_exposure_pct)}
          sub={money(book?.total_exposure)}
        />
        <Stat
          label="Drawdown"
          value={percent(book ? -book.drawdown_pct : null)}
          sub={book ? `from ${money(book.high_water_mark)}` : undefined}
          tone={book && book.drawdown_pct > 0 ? "down" : "flat"}
        />
      </div>

      <ErrorNote error={portfolio.error} />

      <div className="grid cols-2" style={{ marginBottom: "var(--gap)" }}>
        <Panel title="Equity Curve">
          <Sparkline values={points} width={520} height={90} />
          <div className="faint mono" style={{ fontSize: 10.5, marginTop: 6 }}>
            {points.length} stored snapshots
          </div>
        </Panel>

        <Panel title="Exposure By Correlation Group">
          {groups.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Group</th>
                  <th className="num">Exposure</th>
                  <th className="num">% of equity</th>
                </tr>
              </thead>
              <tbody>
                {groups.map(([group, value]) => (
                  <tr key={group}>
                    <td>{group}</td>
                    <td className="num">{money(value)}</td>
                    <td className="num">
                      {percent(book && book.equity > 0 ? (value / book.equity) * 100 : null)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>
              No grouped exposure. Correlation groups are defined in{" "}
              <code>config/risk.yaml</code>; holdings in the same group count
              toward one concentration limit.
            </Empty>
          )}
        </Panel>
      </div>

      <Panel title="Positions" flush>
        {book && book.positions.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th className="num">Qty</th>
                <th className="num">Entry</th>
                <th className="num">Last</th>
                <th className="num">Value</th>
                <th className="num">P&L</th>
                <th className="num">%</th>
                <th>Strategy</th>
              </tr>
            </thead>
            <tbody>
              {book.positions.map((p) => (
                <tr key={p.symbol}>
                  <td className="symbol">{p.symbol}</td>
                  <td>
                    <Badge tone={p.side === "long" ? "ok" : "error"}>{p.side}</Badge>
                  </td>
                  <td className="num">{p.quantity}</td>
                  <td className="num">{money(p.average_entry_price)}</td>
                  <td className="num">{money(p.current_price)}</td>
                  <td className="num">{money(p.market_value)}</td>
                  <td className={`num ${direction(p.unrealized_pl)}`}>
                    {money(p.unrealized_pl)}
                  </td>
                  <td className={`num ${direction(p.unrealized_pl_pct)}`}>
                    {percent(p.unrealized_pl_pct)}
                  </td>
                  <td className="faint">{p.strategy_id ?? "manual"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No open positions.</Empty>
        )}
      </Panel>

      <div style={{ marginTop: "var(--gap)" }}>
        <Panel title="Order History" flush scroll>
          {orders.data && orders.data.orders.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Submitted</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Type</th>
                  <th className="num">Qty</th>
                  <th className="num">Filled</th>
                  <th className="num">Avg Fill</th>
                  <th>Status</th>
                  <th>Strategy</th>
                  <th>Client Order ID</th>
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
                    <td className="faint">{order.order_type}</td>
                    <td className="num">{order.quantity}</td>
                    <td className="num">{order.filled_quantity}</td>
                    <td className="num">{money(order.average_fill_price)}</td>
                    <td>
                      <Badge tone={order.status}>{order.status}</Badge>
                    </td>
                    <td className="faint">{order.strategy_id ?? "—"}</td>
                    <td className="faint" style={{ fontSize: 10 }}>
                      {order.client_order_id}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No orders yet.</Empty>
          )}
        </Panel>
      </div>
    </>
  );
}
