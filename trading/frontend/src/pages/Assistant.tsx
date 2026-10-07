/**
 * ASSISTANT — ask ATLAS about your own account, in plain language.
 *
 * Three decisions worth knowing about:
 *
 * 1.  **It streams.** A thorough answer at high effort can take half a minute,
 *     and a chat that shows nothing for thirty seconds looks broken. The
 *     backend's `/api/assistant/stream` endpoint sends server-sent events; the
 *     reader below is hand-rolled because `EventSource` cannot issue a POST,
 *     and the question has to be a POST (it is not a URL parameter).
 *
 * 2.  **The boundary is on screen, not in a footnote.** The panel on the right
 *     lists what the assistant can and cannot do, read live from the backend
 *     rather than typed here, so it cannot drift from the truth. If someone
 *     ever gave the assistant the ability to trade, this panel would say so.
 *
 * 3.  **Cost is visible.** Every answer shows what it cost and what is left of
 *     the session budget. An assistant that quietly spends money is a bad
 *     surprise, and the number also teaches which questions are expensive.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Empty, ErrorNote, Panel } from "../components/common";
import { api } from "../lib/api";
import { usePoll } from "../lib/hooks";
import type { AssistantStatus } from "../lib/types";

interface Turn {
  role: "you" | "atlas";
  text: string;
  /** False when the text is an explanation of a failure, not an answer. */
  grounded?: boolean;
  degraded?: boolean;
  cost?: number | null;
  seconds?: number;
  streaming?: boolean;
}

const CONVERSATION_ID = "dashboard";

export function Assistant() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Polled slowly: it only changes when a question is asked, and `refresh`
  // is called directly after each one.
  const status = usePoll(api.assistantStatus, 30000);
  const transcript = useRef<HTMLDivElement>(null);

  // Follow the conversation as it grows, including during streaming.
  useEffect(() => {
    const node = transcript.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [turns]);

  const send = useCallback(
    async (question: string) => {
      const trimmed = question.trim();
      if (!trimmed || busy) return;

      setBusy(true);
      setError(null);
      setDraft("");
      setTurns((previous) => [
        ...previous,
        { role: "you", text: trimmed },
        { role: "atlas", text: "", streaming: true },
      ]);

      const started = performance.now();

      /** Replace the trailing placeholder turn. */
      const patch = (update: Partial<Turn>) =>
        setTurns((previous) => {
          const next = [...previous];
          const last = next[next.length - 1];
          if (last && last.role === "atlas") next[next.length - 1] = { ...last, ...update };
          return next;
        });

      try {
        const response = await fetch("/api/assistant/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: trimmed, conversation_id: CONVERSATION_ID }),
        });

        if (!response.ok || !response.body) {
          throw new Error(
            response.status === 0
              ? "Cannot reach the ATLAS backend."
              : `The backend returned ${response.status}.`,
          );
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let text = "";
        let failed = false;

        // Server-sent events are separated by a blank line. A chunk can split
        // anywhere, so whatever is left after the last separator stays in the
        // buffer for the next read.
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";

          for (const frame of frames) {
            let kind = "";
            let data = "";
            for (const line of frame.split("\n")) {
              if (line.startsWith("event: ")) kind = line.slice(7).trim();
              else if (line.startsWith("data: ")) data = line.slice(6);
            }
            if (!kind) continue;

            let payload = data;
            try {
              payload = JSON.parse(data) as string;
            } catch {
              // Keep the raw string rather than dropping the frame.
            }

            if (kind === "chunk") {
              text += payload;
              patch({ text });
            } else if (kind === "error") {
              failed = true;
              patch({ text: payload, degraded: true, grounded: false, streaming: false });
            }
          }
        }

        if (!failed) {
          patch({
            text: text || "(the model returned nothing)",
            grounded: Boolean(text),
            streaming: false,
            seconds: (performance.now() - started) / 1000,
          });
        }
      } catch (cause) {
        // Streaming failed outright. Fall back to the non-streaming endpoint
        // rather than losing the question: a proxy that buffers SSE, or a
        // browser that dropped the connection, should not cost the operator
        // their typed question.
        try {
          const answer = await api.ask(trimmed, CONVERSATION_ID);
          patch({
            text: answer.answer,
            grounded: answer.grounded,
            degraded: answer.degraded,
            cost: answer.cost_usd,
            seconds: answer.elapsed_seconds,
            streaming: false,
          });
        } catch (fallbackCause) {
          const message =
            fallbackCause instanceof Error ? fallbackCause.message : String(cause);
          setError(message);
          patch({ text: message, degraded: true, grounded: false, streaming: false });
        }
      } finally {
        setBusy(false);
        status.refresh();
      }
    },
    [busy, status],
  );

  const reset = async () => {
    await api.assistantReset(CONVERSATION_ID).catch(() => undefined);
    setTurns([]);
    setError(null);
    status.refresh();
  };

  const resetBudget = async () => {
    await api.assistantReset(CONVERSATION_ID, true).catch(() => undefined);
    setTurns([]);
    status.refresh();
  };

  const info = status.data;
  const suggestions = info?.suggested_questions ?? [];

  return (
    <>
      {info && !info.setup.configured ? (
        <div className="banner info">
          <strong>NO AI MODEL CONFIGURED</strong>
          <span>
            The assistant is answering by looking up ATLAS's own figures, so
            every number is real but there is no reasoning behind it. Add{" "}
            <code>{info.setup.env_var}</code> to <code>trading/.env</code> and
            restart the backend for conversational answers.
          </span>
          <span className="spacer" />
          <span className="faint">Nothing else in ATLAS needs a key.</span>
        </div>
      ) : null}

      {info?.budget_exhausted ? (
        <div className="banner warn">
          <strong>AI BUDGET SPENT</strong>
          <span>
            ${info.session_cost_usd.toFixed(2)} of ${info.budget_usd.toFixed(2)} used
            this session.
          </span>
          <span className="spacer" />
          <button className="btn sm" onClick={resetBudget}>
            Reset counter
          </button>
        </div>
      ) : null}

      <div className="assistant-layout">
        <Panel
          title="Ask ATLAS"
          actions={
            <>
              {info ? (
                <span className="faint mono" style={{ fontSize: 10 }}>
                  {info.provider.model} · ${info.session_cost_usd.toFixed(3)} this session
                </span>
              ) : null}
              <button className="btn sm" onClick={reset} disabled={busy || turns.length === 0}>
                Clear
              </button>
            </>
          }
          flush
        >
          <div className="chat" ref={transcript}>
            {turns.length === 0 ? (
              <div className="chat-welcome">
                <h3>What would you like to know?</h3>
                <p className="faint">
                  The assistant reads your account, your risk decisions and your
                  agents, and explains them. It cannot trade.
                </p>
                <div className="suggestions">
                  {suggestions.map((question) => (
                    <button
                      key={question}
                      className="suggestion"
                      onClick={() => void send(question)}
                      disabled={busy}
                    >
                      {question}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              turns.map((turn, index) => (
                <div
                  key={index}
                  className={`bubble ${turn.role}${turn.degraded ? " degraded" : ""}`}
                >
                  <div className="bubble-head">
                    <span className="bubble-who">
                      {turn.role === "you" ? "You" : "ATLAS"}
                    </span>
                    {turn.degraded ? <Badge tone="warn">unavailable</Badge> : null}
                    {turn.role === "atlas" && !turn.degraded && turn.grounded ? (
                      <Badge tone="ok">from ATLAS data</Badge>
                    ) : null}
                    <span className="spacer" />
                    {turn.seconds ? (
                      <span className="faint mono" style={{ fontSize: 10 }}>
                        {turn.seconds.toFixed(1)}s
                        {turn.cost ? ` · $${turn.cost.toFixed(4)}` : ""}
                      </span>
                    ) : null}
                  </div>
                  <div className="bubble-body">
                    {turn.text}
                    {turn.streaming ? <span className="caret" /> : null}
                  </div>
                </div>
              ))
            )}
          </div>

          <form
            className="chat-input"
            onSubmit={(event) => {
              event.preventDefault();
              void send(draft);
            }}
          >
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                // Enter sends, Shift+Enter makes a new line. Standard for a
                // chat box and what a one-line question wants.
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void send(draft);
                }
              }}
              placeholder="Ask about your account, your risk limits, what the scanner found..."
              rows={2}
              disabled={busy}
            />
            <button className="btn primary" type="submit" disabled={busy || !draft.trim()}>
              {busy ? "Thinking..." : "Ask"}
            </button>
          </form>

          <ErrorNote error={error} />
        </Panel>

        <div className="assistant-side">
          <Panel title="What it can do">
            {info ? <Boundary status={info} /> : <Empty>Loading...</Empty>}
          </Panel>

          <Panel title="Not financial advice">
            <p className="faint" style={{ margin: 0, fontSize: 12.5, lineHeight: 1.6 }}>
              The assistant explains what ATLAS measured. It does not know your
              circumstances, it has no track record, and it is wrong sometimes.
              Every decision that risks money is yours, made at this dashboard,
              behind the Risk Officer's veto.
            </p>
            <p className="faint" style={{ marginBottom: 0, fontSize: 12.5, lineHeight: 1.6 }}>
              If an answer states a number you cannot find elsewhere in the
              dashboard, distrust the answer, not the dashboard.
            </p>
          </Panel>
        </div>
      </div>
    </>
  );
}

/**
 * The capability panel.
 *
 * Read from the backend rather than written here. If the assistant's powers
 * ever changed, this would change with them instead of quietly lying.
 */
function Boundary({ status }: { status: AssistantStatus }) {
  const can = [
    ["Read your account and positions", status.capabilities.can_read_account],
    ["Explain your risk limits and vetoes", status.capabilities.can_explain_risk],
  ] as const;

  const cannot = [
    ["Place, change or cancel an order", status.capabilities.can_place_orders],
    ["Change a risk limit", status.capabilities.can_change_risk_limits],
    ["Enable a strategy", status.capabilities.can_enable_strategies],
    ["Release the kill switch", status.capabilities.can_release_kill_switch],
    ["See your Alpaca keys", status.capabilities.holds_broker_credentials],
  ] as const;

  return (
    <div className="boundary">
      <ul className="boundary-list">
        {can.map(([label, allowed]) => (
          <li key={label} className={allowed ? "yes" : "no"}>
            <span className="mark">{allowed ? "+" : "x"}</span>
            {label}
          </li>
        ))}
        {cannot.map(([label, allowed]) => (
          <li key={label} className={allowed ? "yes warnrow" : "no"}>
            <span className="mark">{allowed ? "!" : "x"}</span>
            {label}
          </li>
        ))}
      </ul>

      <div className="boundary-note">
        Enforced in code, not by asking the model nicely: the Assistant Agent is
        never given a broker, a risk evaluator or a strategy registry, and no
        tools are sent with any request.
      </div>

      <table className="kv">
        <tbody>
          <tr>
            <td>Provider</td>
            <td className="mono">{status.provider.provider}</td>
          </tr>
          <tr>
            <td>Model</td>
            <td className="mono">{status.provider.model}</td>
          </tr>
          <tr>
            <td>Questions</td>
            <td className="mono">{status.questions_answered}</td>
          </tr>
          <tr>
            <td>Session cost</td>
            <td className="mono">${status.session_cost_usd.toFixed(4)}</td>
          </tr>
          <tr>
            <td>Budget left</td>
            <td className="mono">
              {status.budget_remaining_usd === null
                ? "unlimited"
                : `$${status.budget_remaining_usd.toFixed(3)}`}
            </td>
          </tr>
        </tbody>
      </table>

      {status.last_error ? <div className="error-text">{status.last_error}</div> : null}
    </div>
  );
}
