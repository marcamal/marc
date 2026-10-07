/** STRATEGIES — enable, disable, inspect. Everything ships off. */

import { api } from "../lib/api";
import { time } from "../lib/format";
import { useAction, usePoll } from "../lib/hooks";
import { Badge, Empty, ErrorNote, Panel } from "../components/common";

export function Strategies() {
  const strategies = usePoll(api.strategies, 6000);
  const { run, pending, error } = useAction();

  const data = strategies.data;

  return (
    <>
      <div className="banner warn">
        <strong>NOTHING HERE IS PROVEN</strong>
        <span>
          {data?.warning ??
            "No strategy has been backtested. Enabling one lets it generate proposals, which the Risk Officer then judges."}
        </span>
      </div>

      {data && !data.globally_enabled ? (
        <div className="banner info">
          <strong>MASTER SWITCH IS OFF</strong>
          <span>
            <code>strategies_globally_enabled: false</code> in{" "}
            <code>config/strategies.yaml</code>. No strategy can run, whatever
            its own flag says. That switch is deliberately file-only — there is
            no button for it.
          </span>
        </div>
      ) : null}

      <ErrorNote error={error ?? strategies.error} />

      <div className="grid cols-2">
        {(data?.strategies ?? []).map((strategy) => (
          <Panel
            key={strategy.id}
            title={strategy.name}
            actions={
              <div className="row">
                <Badge tone={strategy.enabled ? "ok" : "off"}>
                  {strategy.enabled ? "enabled" : "disabled"}
                </Badge>
                <button
                  className={`btn sm ${strategy.enabled ? "" : "primary"}`}
                  disabled={pending}
                  onClick={() =>
                    run(async () => {
                      if (strategy.enabled) {
                        await api.disableStrategy(strategy.id);
                      } else {
                        await api.enableStrategy(strategy.id);
                      }
                      strategies.refresh();
                    })
                  }
                >
                  {strategy.enabled ? "Disable" : "Enable"}
                </button>
              </div>
            }
          >
            <div className="hint" style={{ marginBottom: 10 }}>
              {strategy.description}
            </div>

            <dl className="kv">
              <dt>id</dt>
              <dd>{strategy.id}</dd>
              <dt>timeframe</dt>
              <dd>{strategy.timeframe}</dd>
              <dt>symbols</dt>
              <dd>{strategy.symbols.join(", ") || "—"}</dd>
              <dt>proposals made</dt>
              <dd>{strategy.proposals_generated}</dd>
              <dt>last run</dt>
              <dd>{time(strategy.last_run_at)}</dd>
              {strategy.last_error ? (
                <>
                  <dt>last error</dt>
                  <dd className="down">{strategy.last_error}</dd>
                </>
              ) : null}
            </dl>

            <div className="panel-title" style={{ margin: "14px 0 6px" }}>
              Parameters
            </div>
            <pre className="block">{JSON.stringify(strategy.params, null, 2)}</pre>

            <div className="hint" style={{ marginTop: 8 }}>
              Enabling here lasts for this session only and is not written back
              to <code>strategies.yaml</code>, so a restart returns to the
              configured state.
            </div>
          </Panel>
        ))}
      </div>

      {data && data.strategies.length === 0 ? (
        <Empty>
          No strategies loaded. Add one to <code>config/strategies.yaml</code>.
        </Empty>
      ) : null}
    </>
  );
}
