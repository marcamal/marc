/** SYSTEM — connections, capabilities, logs and the live event feed. */

import { api } from "../lib/api";
import { bytes, duration, humanise, time } from "../lib/format";
import { usePoll } from "../lib/hooks";
import type { BusEvent, SystemStatus } from "../lib/types";
import { Badge, Empty, ErrorNote, Indicator, Panel } from "../components/common";

export function System({
  status,
  events,
  socketConnected,
}: {
  status: SystemStatus | null;
  events: BusEvent[];
  socketConnected: boolean;
}) {
  const health = usePoll(api.health, 8000);
  const capabilities = usePoll(api.capabilities, 30000);
  const logs = usePoll(() => api.logs(200), 5000);

  return (
    <>
      <div className="grid cols-2" style={{ marginBottom: "var(--gap)" }}>
        <Panel title="Connections">
          <div className="row wrap" style={{ gap: 18, marginBottom: 12 }}>
            <Indicator
              label="Backend"
              state={health.data?.status === "ok" ? "ok" : "warn"}
            />
            <Indicator
              label="Broker"
              state={health.data?.components.broker ? "ok" : "error"}
            />
            <Indicator
              label="Database"
              state={health.data?.components.database ? "ok" : "error"}
            />
            <Indicator
              label="Event Bus"
              state={health.data?.components.event_bus ? "ok" : "error"}
            />
            <Indicator
              label="Agents"
              state={health.data?.components.agents ? "ok" : "warn"}
            />
            <Indicator label="UI Socket" state={socketConnected ? "ok" : "off"} />
          </div>

          <dl className="kv">
            <dt>mode</dt>
            <dd>
              {status?.mode.effective} (requested {status?.mode.requested})
            </dd>
            <dt>broker</dt>
            <dd>
              {status?.broker.name} — {status?.broker.endpoint}
              {status?.broker.simulated ? " (simulated)" : ""}
            </dd>
            <dt>credentials</dt>
            <dd>
              {status?.broker.has_credentials ? (
                <span className="up">present</span>
              ) : (
                <span className="warn-text faint">
                  not set — add them to trading/.env
                </span>
              )}
            </dd>
            <dt>data feed</dt>
            <dd>
              {status?.market_data.feed}
              {status?.broker.capabilities?.has_paid_data_plan === false
                ? " (free plan)"
                : ""}
            </dd>
            <dt>streaming</dt>
            <dd>
              {status?.market_data.symbol_count ?? 0} symbols
              {status?.market_data.seconds_since_last_message !== null &&
              status?.market_data.seconds_since_last_message !== undefined
                ? ` · last message ${duration(status.market_data.seconds_since_last_message)} ago`
                : " · no stream"}
            </dd>
            <dt>market</dt>
            <dd>
              {status?.market_clock?.is_open ? (
                <span className="up">open</span>
              ) : (
                <span className="faint">closed</span>
              )}
            </dd>
            <dt>uptime</dt>
            <dd>{duration(status?.uptime_seconds)}</dd>
            <dt>database</dt>
            <dd>
              {bytes(status?.database.size_bytes)} ·{" "}
              {status?.database.recorder.rows_written ?? 0} rows written
              {status && status.database.recorder.write_errors > 0 ? (
                <span className="down">
                  {" "}
                  · {status.database.recorder.write_errors} write errors
                </span>
              ) : null}
            </dd>
            <dt>event bus</dt>
            <dd>
              {status?.event_bus.published_total ?? 0} published
              {status && status.event_bus.total_dropped > 0 ? (
                <span className="warn"> · {status.event_bus.total_dropped} dropped</span>
              ) : null}
            </dd>
            <dt>orders today</dt>
            <dd>
              {status?.orders.orders_today ?? 0} · {status?.orders.open_orders ?? 0} open
            </dd>
          </dl>
        </Panel>

        <Panel title="Broker Capabilities — detected, not assumed">
          {capabilities.data ? (
            <>
              <div className="hint" style={{ marginBottom: 10 }}>
                ATLAS asks the account what it can do rather than assuming.
                Product availability depends on the broker and on your country
                of residence.
              </div>

              <div className="row wrap" style={{ gap: 6, marginBottom: 12 }}>
                {Object.entries(capabilities.data.capabilities)
                  .filter(([key, value]) => key.startsWith("supports_") && typeof value === "boolean")
                  .map(([key, value]) => (
                    <Badge key={key} tone={value ? "ok" : "off"}>
                      {key.replace("supports_", "").replace(/_/g, " ")}
                    </Badge>
                  ))}
                <Badge tone={capabilities.data.capabilities.margin_enabled ? "ok" : "off"}>
                  margin
                </Badge>
              </div>

              {capabilities.data.unsupported.length > 0 ? (
                <>
                  <div className="panel-title" style={{ marginBottom: 6 }}>
                    Unsupported by current broker
                  </div>
                  <ul className="hint" style={{ margin: "0 0 12px", paddingLeft: 18 }}>
                    {capabilities.data.unsupported.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </>
              ) : null}

              {capabilities.data.notes.length > 0 ? (
                <>
                  <div className="panel-title" style={{ marginBottom: 6 }}>
                    Notes
                  </div>
                  <ul className="hint" style={{ margin: 0, paddingLeft: 18 }}>
                    {capabilities.data.notes.map((note, index) => (
                      <li key={index}>{note}</li>
                    ))}
                  </ul>
                </>
              ) : null}
            </>
          ) : (
            <Empty>Loading capabilities…</Empty>
          )}
        </Panel>
      </div>

      {status && !status.mode.is_live ? (
        <Panel title="Live Trading Gates" >
          <div className="hint" style={{ marginBottom: 10 }}>
            Live trading needs all three of these, set in{" "}
            <code>trading/.env</code>, and a restart. There is deliberately no
            button for it anywhere in this interface.
          </div>
          <div className="row wrap" style={{ gap: 8 }}>
            {Object.entries(status.mode.live_gates).map(([gate, open]) => (
              <Badge key={gate} tone={open ? "ok" : "off"}>
                {humanise(gate)}: {open ? "open" : "closed"}
              </Badge>
            ))}
          </div>
        </Panel>
      ) : null}

      <div className="grid cols-2" style={{ marginTop: "var(--gap)" }}>
        <Panel
          title="Logs"
          actions={
            <span className="faint mono" style={{ fontSize: 10 }}>
              {logs.data?.logs.length ?? 0} recent
            </span>
          }
          scroll
        >
          {logs.data && logs.data.logs.length > 0 ? (
            <div className="logs">
              {logs.data.logs.map((line, index) => (
                <div className="log-line" key={`${line.timestamp}-${index}`}>
                  <span className="log-time">{time(line.timestamp)}</span>
                  <span className={`log-level ${line.level}`}>{line.level}</span>
                  <span className="log-logger">{line.logger.replace("app.", "")}</span>
                  <span style={{ whiteSpace: "normal" }}>{line.message}</span>
                </div>
              ))}
            </div>
          ) : (
            <Empty>No log records.</Empty>
          )}
        </Panel>

        <Panel
          title="Live Event Feed"
          actions={<Indicator label="socket" state={socketConnected ? "ok" : "off"} />}
          scroll
        >
          {events.length > 0 ? (
            <div className="logs">
              {events.map((event) => (
                <div className="log-line" key={event.id}>
                  <span className="log-time">{time(event.timestamp)}</span>
                  <span className="log-logger">{event.topic}</span>
                  <span className="faint">{String(event.source ?? "")}</span>
                  {event.trace_id ? (
                    <span className="faint" style={{ fontSize: 10 }}>
                      trace {String(event.trace_id).slice(0, 10)}
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
          ) : (
            <Empty>
              Waiting for events. Bars, risk verdicts, orders and agent status
              changes appear here as they happen.
            </Empty>
          )}
        </Panel>
      </div>

      <ErrorNote error={health.error} />
    </>
  );
}
