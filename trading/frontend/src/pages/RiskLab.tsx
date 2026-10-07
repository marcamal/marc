/**
 * RISK — the limits, the recent verdicts, and a read-only trade checker.
 *
 * The checker is a teaching tool: it runs a hypothetical trade past the real
 * Risk Agent and shows every rule with its limit and the measured value, so
 * you can see *why* something would be blocked before trying it. It calls
 * `RiskAgent.check()`, which evaluates without publishing, so it cannot
 * produce an order.
 */

import { useState } from "react";
import { api } from "../lib/api";
import { humanise, money, number, time } from "../lib/format";
import { useAction, usePoll } from "../lib/hooks";
import type { RiskCheck, RiskDecision } from "../lib/types";
import { Badge, Empty, ErrorNote, Panel } from "../components/common";

export function RiskLab() {
  const status = usePoll(api.riskStatus, 8000);
  const decisions = usePoll(() => api.riskDecisions(20), 8000);
  const events = usePoll(() => api.riskEvents(20), 15000);

  return (
    <>
      <div className="grid cols-2" style={{ marginBottom: "var(--gap)" }}>
        <TradeChecker />

        <Panel title="Active Limits">
          {status.data ? (
            <>
              <dl className="kv">
                {Object.entries(status.data.config)
                  .filter(([, value]) => typeof value !== "object")
                  .map(([key, value]) => (
                    <div key={key} style={{ display: "contents" }}>
                      <dt>{humanise(key)}</dt>
                      <dd>{String(value)}</dd>
                    </div>
                  ))}
              </dl>

              <div className="panel-title" style={{ margin: "16px 0 6px" }}>
                Hard ceilings (config cannot exceed these)
              </div>
              <dl className="kv">
                {Object.entries(status.data.hard_ceilings).map(([key, value]) => (
                  <div key={key} style={{ display: "contents" }}>
                    <dt>{humanise(key)}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
              <div className="hint" style={{ marginTop: 10 }}>
                These are compiled into the code. Editing{" "}
                <code>config/risk.yaml</code> can only ever make ATLAS more
                conservative — a value above a ceiling is clamped, and logged.
              </div>
            </>
          ) : (
            <Empty>Loading…</Empty>
          )}
        </Panel>
      </div>

      <div className="grid cols-2">
        <Panel title="Recent Risk Decisions" scroll>
          {decisions.data && decisions.data.decisions.length > 0 ? (
            decisions.data.decisions.map((decision) => (
              <DecisionBlock key={`${decision.proposal_id}-${decision.decided_at}`} decision={decision} />
            ))
          ) : (
            <Empty>
              No decisions yet. The Risk Officer records every verdict here,
              approvals and vetoes alike.
            </Empty>
          )}
        </Panel>

        <Panel title="Risk Events" flush scroll>
          {events.data && events.data.events.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Severity</th>
                  <th>Rule</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {events.data.events.map((event, index) => (
                  <tr key={index}>
                    <td className="faint">{time(String(event.created_at ?? ""))}</td>
                    <td>
                      <Badge tone={event.severity === "critical" ? "error" : "warn"}>
                        {String(event.severity)}
                      </Badge>
                    </td>
                    <td>{String(event.rule)}</td>
                    <td className="faint" style={{ whiteSpace: "normal" }}>
                      {String(event.detail)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No risk events recorded.</Empty>
          )}
        </Panel>
      </div>

      <ErrorNote error={status.error} />
    </>
  );
}

function DecisionBlock({ decision }: { decision: RiskDecision }) {
  const [open, setOpen] = useState(false);
  const tone = decision.is_approved
    ? decision.decision === "approved_reduced"
      ? "warn"
      : "approved"
    : "rejected";

  return (
    <div style={{ marginBottom: 10, paddingBottom: 8, borderBottom: "1px solid var(--border)" }}>
      <div className="row">
        <Badge tone={tone}>{decision.decision}</Badge>
        <span className="mono faint" style={{ fontSize: 11 }}>
          {decision.proposal_id}
        </span>
        <span className="spacer" />
        <span className="faint mono" style={{ fontSize: 10 }}>
          {time(decision.decided_at)}
        </span>
        <button className="btn sm" onClick={() => setOpen(!open)}>
          {open ? "Hide" : `${decision.checks.length} checks`}
        </button>
      </div>

      {decision.reasons.length > 0 ? (
        <div className="error-text" style={{ marginTop: 5 }}>
          {decision.reasons.join(" · ")}
        </div>
      ) : null}

      {open ? <CheckList checks={decision.checks} /> : null}
    </div>
  );
}

function CheckList({ checks }: { checks: RiskCheck[] }) {
  return (
    <div style={{ marginTop: 8 }}>
      {checks.map((check) => (
        <div className="check-row" key={check.rule}>
          <span className={`verdict ${check.passed ? "up" : "down"}`}>
            {check.passed ? "PASS" : "FAIL"}
          </span>
          <span className="rule">{check.rule}</span>
          <span className="faint" style={{ whiteSpace: "normal" }}>
            {check.detail}
          </span>
        </div>
      ))}
    </div>
  );
}

/** The read-only "would this be allowed?" form. */
function TradeChecker() {
  const [form, setForm] = useState({
    symbol: "SPY",
    side: "buy" as "buy" | "sell",
    quantity: "1",
    entry_price: "585",
    stop_price: "580",
    target_price: "600",
  });
  const [result, setResult] = useState<
    { decision: RiskDecision; summary: string; note: string } | null
  >(null);
  const { run, pending, error } = useAction();

  const update = (key: keyof typeof form) => (
    event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>,
  ) => setForm({ ...form, [key]: event.target.value });

  const submit = async () => {
    setResult(null);
    await run(async () => {
      const response = await api.checkTrade({
        symbol: form.symbol.toUpperCase(),
        side: form.side,
        quantity: Number(form.quantity),
        entry_price: Number(form.entry_price),
        stop_price: Number(form.stop_price),
        target_price: form.target_price ? Number(form.target_price) : null,
      });
      setResult({
        decision: response.decision,
        summary: response.summary,
        note: response.note,
      });
    });
  };

  const risk =
    Number(form.quantity) * Math.abs(Number(form.entry_price) - Number(form.stop_price));

  return (
    <Panel
      title="Trade Checker — read only"
      actions={
        <span className="faint mono" style={{ fontSize: 10 }}>
          places no order
        </span>
      }
    >
      <div className="hint" style={{ marginBottom: 10 }}>
        Run a hypothetical trade past the real Risk Officer. Every rule is shown
        with its limit and the measured value. Nothing is submitted.
      </div>

      <div className="grid" style={{ gridTemplateColumns: "repeat(3, 1fr)", gap: 10 }}>
        <div>
          <label className="field-label">Symbol</label>
          <input className="field" value={form.symbol} onChange={update("symbol")} />
        </div>
        <div>
          <label className="field-label">Side</label>
          <select className="field" value={form.side} onChange={update("side")}>
            <option value="buy">buy</option>
            <option value="sell">sell</option>
          </select>
        </div>
        <div>
          <label className="field-label">Quantity</label>
          <input className="field" value={form.quantity} onChange={update("quantity")} />
        </div>
        <div>
          <label className="field-label">Entry</label>
          <input className="field" value={form.entry_price} onChange={update("entry_price")} />
        </div>
        <div>
          <label className="field-label">Stop</label>
          <input className="field" value={form.stop_price} onChange={update("stop_price")} />
        </div>
        <div>
          <label className="field-label">Target</label>
          <input className="field" value={form.target_price} onChange={update("target_price")} />
        </div>
      </div>

      <div className="row" style={{ marginTop: 12 }}>
        <button className="btn primary" onClick={submit} disabled={pending}>
          {pending ? "Checking…" : "Check Trade"}
        </button>
        <span className="faint mono" style={{ fontSize: 11 }}>
          risk if stopped: {money(Number.isFinite(risk) ? risk : null)}
        </span>
      </div>

      <ErrorNote error={error} />

      {result ? (
        <div style={{ marginTop: 14 }}>
          <div className="row">
            <Badge tone={result.decision.is_approved ? "approved" : "rejected"}>
              {result.decision.decision}
            </Badge>
            <span className="mono" style={{ fontSize: 12 }}>
              {result.summary}
            </span>
          </div>
          {result.decision.approved_quantity !== null ? (
            <div className="hint" style={{ marginTop: 4 }}>
              Approved quantity: {number(result.decision.approved_quantity, 0)}
            </div>
          ) : null}
          <CheckList checks={result.decision.checks} />
          <div className="hint" style={{ marginTop: 8 }}>{result.note}</div>
        </div>
      ) : null}
    </Panel>
  );
}
