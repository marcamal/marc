/** SCANNER — ranked candidates. Observations, never instructions. */

import { api } from "../lib/api";
import { compact, money, number, percent, direction } from "../lib/format";
import { useAction, usePoll } from "../lib/hooks";
import { Badge, Empty, ErrorNote, Panel, ScoreBar } from "../components/common";

export function Scanner() {
  const results = usePoll(() => api.scannerResults(30), 10000);
  const universes = usePoll(api.universes, 60000);
  const { run, pending, error } = useAction();

  const data = results.data;
  const excluded = Object.entries(data?.excluded ?? {});

  return (
    <>
      <div className="banner info">
        <strong>SCANNER OUTPUT IS NOT A TRADE SIGNAL</strong>
        <span>{data?.disclaimer ?? "Scores rank the symbols in this scan against each other."}</span>
      </div>

      <Panel
        title={`Ranked Candidates — ${data?.universe ?? ""}`}
        flush
        actions={
          <div className="row">
            <span className="faint mono" style={{ fontSize: 10 }}>
              {data?.results.length ?? 0} of {data?.universe_size ?? 0} scanned
            </span>
            <button
              className="btn sm primary"
              disabled={pending}
              onClick={() => run(async () => {
                await api.runScan();
                results.refresh();
              })}
            >
              {pending ? "Scanning…" : "Scan Now"}
            </button>
          </div>
        }
      >
        {data && data.results.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Symbol</th>
                <th>Score</th>
                <th className="num">Price</th>
                <th className="num">Change</th>
                <th className="num">RVol</th>
                <th className="num">Mom</th>
                <th className="num">RSI</th>
                <th className="num">ATR%</th>
                <th className="num">VWAP</th>
                <th className="num">Spread</th>
                <th className="num">Volume</th>
                <th>Trend</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {data.results.map((r) => (
                <tr key={r.symbol}>
                  <td className="faint">{r.rank}</td>
                  <td className="symbol">{r.symbol}</td>
                  <td>
                    <ScoreBar score={r.score} />
                  </td>
                  <td className="num">{money(r.price)}</td>
                  <td className={`num ${direction(r.percent_change)}`}>
                    {percent(r.percent_change)}
                  </td>
                  <td className="num">{number(r.relative_volume)}</td>
                  <td className={`num ${direction(r.momentum_pct)}`}>
                    {percent(r.momentum_pct)}
                  </td>
                  <td className="num">{number(r.rsi, 0)}</td>
                  <td className="num">{number(r.atr_percent)}</td>
                  <td className={`num ${direction(r.distance_from_vwap_pct)}`}>
                    {percent(r.distance_from_vwap_pct)}
                  </td>
                  <td className="num faint">{number(r.spread_percent, 3)}</td>
                  <td className="num faint">{compact(r.volume)}</td>
                  <td>
                    {r.ema_stack_bullish === null ? (
                      <span className="faint">—</span>
                    ) : (
                      <Badge tone={r.ema_stack_bullish ? "ok" : "off"}>
                        {r.ema_stack_bullish ? "bullish" : "mixed"}
                      </Badge>
                    )}
                  </td>
                  <td className="faint" style={{ whiteSpace: "normal", minWidth: 200 }}>
                    {r.notes.join(" · ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>
            {data?.agent_status === "stopped"
              ? "The scanner agent is stopped. Start it on the Agents page, or press Scan Now."
              : "No candidates passed the filters."}
          </Empty>
        )}
      </Panel>

      <ErrorNote error={error ?? results.error} />

      <div className="grid cols-2" style={{ marginTop: "var(--gap)" }}>
        <Panel title="Excluded By Filters" scroll>
          {excluded.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Failed</th>
                </tr>
              </thead>
              <tbody>
                {excluded.map(([symbol, reasons]) => (
                  <tr key={symbol}>
                    <td className="symbol">{symbol}</td>
                    <td className="faint" style={{ whiteSpace: "normal" }}>
                      {reasons.join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>Nothing was excluded.</Empty>
          )}
        </Panel>

        <Panel title="Universes">
          {universes.data ? (
            <>
              <div className="hint" style={{ marginBottom: 10 }}>
                {universes.data.note}
              </div>
              <dl className="kv">
                {Object.entries(universes.data.sizes).map(([name, size]) => (
                  <div key={name} style={{ display: "contents" }}>
                    <dt>
                      {name === universes.data!.active ? "● " : ""}
                      {name}
                    </dt>
                    <dd>
                      {size} symbols
                      {size > universes.data!.streaming_limit ? (
                        <span className="faint">
                          {" "}
                          (only {universes.data!.streaming_limit} stream live)
                        </span>
                      ) : null}
                    </dd>
                  </div>
                ))}
              </dl>
              <div className="hint" style={{ marginTop: 10 }}>
                Change the active universe in <code>config/scanner.yaml</code>.
              </div>
            </>
          ) : (
            <Empty>Loading…</Empty>
          )}
        </Panel>
      </div>
    </>
  );
}
