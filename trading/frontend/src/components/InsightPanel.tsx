/**
 * The AI insight panel on the Command Center.
 *
 * It does NOT fetch on mount. A briefing costs tokens, and a dashboard left
 * open all day that quietly generated one every poll interval would be an
 * unpleasant surprise on the bill - so the operator presses the button. The
 * backend makes the same decision for the same reason: `/api/assistant/briefing`
 * is a POST rather than a GET precisely so nothing can prefetch it.
 *
 * Without an API key this still works. The deterministic provider answers from
 * ATLAS's measured figures, and the panel says which kind of answer it got.
 */

import { useState } from "react";
import { api } from "../lib/api";
import type { AssistantAnswer } from "../lib/types";
import { Badge, Panel } from "./common";

export function InsightPanel({ onOpenAssistant }: { onOpenAssistant?: () => void }) {
  const [answer, setAnswer] = useState<AssistantAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const brief = async () => {
    setBusy(true);
    setError(null);
    try {
      setAnswer(await api.aiBriefing());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title="AI Briefing"
      actions={
        <>
          {answer?.cost_usd ? (
            <span className="faint mono" style={{ fontSize: 10 }}>
              ${answer.cost_usd.toFixed(4)}
            </span>
          ) : null}
          <button className="btn sm" onClick={brief} disabled={busy}>
            {busy ? "Thinking..." : answer ? "Refresh" : "Brief me"}
          </button>
          {onOpenAssistant ? (
            <button className="btn sm" onClick={onOpenAssistant}>
              Ask
            </button>
          ) : null}
        </>
      }
    >
      {error ? <div className="error-text">{error}</div> : null}

      {answer ? (
        <>
          <div className="row" style={{ marginBottom: 8 }}>
            {answer.degraded ? (
              <Badge tone="warn">unavailable</Badge>
            ) : (
              <Badge tone="ok">from ATLAS data</Badge>
            )}
            <span className="faint mono" style={{ fontSize: 10 }}>
              {answer.model} · {answer.elapsed_seconds.toFixed(1)}s
            </span>
          </div>
          <div className="insight-body">{answer.answer}</div>
        </>
      ) : !error ? (
        <div className="insight-idle">
          <p>
            A short written read on where the account and the system stand:
            mode, holdings, anything blocked, any unhealthy agent, and the one
            thing worth looking at next.
          </p>
          <p className="faint">
            Not generated automatically - a briefing costs tokens, so it runs
            when you ask for it. Works without an API key too, using ATLAS's own
            figures.
          </p>
        </div>
      ) : null}
    </Panel>
  );
}
