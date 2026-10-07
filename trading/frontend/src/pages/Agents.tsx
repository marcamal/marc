/**
 * AGENTS — the network, and what each agent is doing and why.
 *
 * The pipeline strip is the shape from the brief:
 *   ORCHESTRATOR → DATA → SCANNER → RESEARCH → STRATEGY → RISK → EXECUTION → ALPACA
 * Its node states come from the live agents, not from a static picture.
 */

import { useState } from "react";
import { api } from "../lib/api";
import { duration, humanise, time } from "../lib/format";
import { useAction, usePoll } from "../lib/hooks";
import type { AgentDescriptor, AgentStatusValue } from "../lib/types";
import { Badge, Empty, ErrorNote, Panel } from "../components/common";

/** Canonical left-to-right order of the trade pipeline. */
const PIPELINE = [
  "orchestrator",
  "market_data",
  "scanner",
  "technical",
  "strategy",
  "risk",
  "execution",
  "broker",
] as const;

const PROTECTED = new Set(["risk", "execution"]);

function nodeClass(status: AgentStatusValue | "broker"): string {
  if (status === "broker") return "pipeline-node broker";
  const running = ["idle", "working", "unhealthy"].includes(status);
  return `pipeline-node${running ? " running" : ""}`;
}

export function Agents() {
  const agents = usePoll(() => api.agents(8), 4000);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { run, pending, error } = useAction();

  const list = agents.data?.agents ?? [];
  const byId = new Map(list.map((a) => [a.id, a]));
  const selected = selectedId ? byId.get(selectedId) : undefined;

  const toggle = async (agent: AgentDescriptor) => {
    const running = ["idle", "working", "unhealthy"].includes(agent.status);
    await run(async () => {
      if (running) {
        await api.stopAgent(agent.id);
      } else {
        await api.startAgent(agent.id);
      }
      agents.refresh();
    });
  };

  return (
    <>
      <Panel
        title="Agent Network"
        actions={
          <span className="faint mono" style={{ fontSize: 10 }}>
            {agents.data?.agents.filter((a) => a.status !== "stopped").length ?? 0} active
          </span>
        }
      >
        <div className="pipeline">
          {PIPELINE.map((id, index) => {
            const agent = byId.get(id);
            const isBroker = id === "broker";
            return (
              <div className="row" key={id} style={{ gap: 0 }}>
                <div
                  className={nodeClass(isBroker ? "broker" : (agent?.status ?? "stopped"))}
                  onClick={() => (agent ? setSelectedId(agent.id) : undefined)}
                  style={{ cursor: agent ? "pointer" : "default" }}
                  title={agent?.role ?? "The broker — where the pipeline ends"}
                >
                  <div className="label">
                    {isBroker ? "Alpaca" : (agent?.name ?? humanise(id))}
                  </div>
                  <div className="state">
                    {isBroker ? "broker" : (agent?.status ?? "not registered")}
                  </div>
                </div>
                {index < PIPELINE.length - 1 ? (
                  <span className="pipeline-arrow">→</span>
                ) : null}
              </div>
            );
          })}
        </div>
        <div className="hint" style={{ marginTop: 10 }}>
          A trade proposal travels left to right. Nothing reaches Alpaca without
          passing the Risk Officer, and only the Execution agent may place an
          order. Click a node to inspect it.
        </div>
      </Panel>

      <ErrorNote error={error ?? agents.error} />

      <div className="grid cols-3" style={{ margin: "var(--gap) 0" }}>
        {list.map((agent) => {
          const running = ["idle", "working", "unhealthy"].includes(agent.status);
          const isProtected = PROTECTED.has(agent.id);
          return (
            <div
              className={`agent-card${selectedId === agent.id ? " selected" : ""}`}
              key={agent.id}
            >
              <div className="row between">
                <span className="agent-name">{agent.name}</span>
                <Badge tone={agent.status}>{agent.status}</Badge>
              </div>

              <div className="agent-role">{agent.role}</div>

              <div className="agent-task">{agent.current_task ?? "idle"}</div>

              <div className="agent-meta">
                <span>cycles {agent.stats.cycles_completed}</span>
                <span>events {agent.stats.events_handled}</span>
                {agent.stats.errors > 0 ? (
                  <span className="down">errors {agent.stats.errors}</span>
                ) : null}
                {agent.confidence !== null ? (
                  <span>conf {(agent.confidence * 100).toFixed(0)}%</span>
                ) : null}
                <span>up {duration(agent.stats.uptime_seconds)}</span>
              </div>

              <div className="row">
                <button
                  className="btn sm"
                  onClick={() => setSelectedId(agent.id === selectedId ? null : agent.id)}
                >
                  {selectedId === agent.id ? "Hide" : "Inspect"}
                </button>
                <button
                  className={`btn sm ${running ? "" : "primary"}`}
                  onClick={() => toggle(agent)}
                  disabled={pending || (running && isProtected)}
                  title={
                    running && isProtected
                      ? "Protected: ATLAS will not run without this agent. Use the kill switch to halt trading."
                      : undefined
                  }
                >
                  {running ? (isProtected ? "Protected" : "Stop") : "Start"}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {selected ? <AgentDetail agent={selected} /> : null}
    </>
  );
}

function AgentDetail({ agent }: { agent: AgentDescriptor }) {
  const detail = usePoll(() => api.agent(agent.id), 4000, [agent.id]);

  return (
    <div className="grid cols-2">
      <Panel title={`${agent.name} — Contract`}>
        <dl className="kv">
          <dt>id</dt>
          <dd>{agent.id}</dd>
          <dt>type</dt>
          <dd>{agent.type}</dd>
          <dt>status</dt>
          <dd>{agent.status}</dd>
          <dt>doing</dt>
          <dd>{agent.current_task ?? "—"}</dd>
          <dt>last activity</dt>
          <dd>{time(agent.last_activity)}</dd>
          <dt>subscribes to</dt>
          <dd>{agent.subscriptions.length ? agent.subscriptions.join(", ") : "—"}</dd>
          <dt>publishes</dt>
          <dd>{agent.outputs.length ? agent.outputs.join(", ") : "—"}</dd>
          <dt>autostart</dt>
          <dd>{agent.autostart ? "yes" : "no"}</dd>
          {agent.stats.last_error ? (
            <>
              <dt>last error</dt>
              <dd className="down">{agent.stats.last_error}</dd>
            </>
          ) : null}
        </dl>

        <div className="panel-title" style={{ margin: "14px 0 6px" }}>
          Detail
        </div>
        <pre className="block">
          {detail.data ? JSON.stringify(detail.data.detail, null, 2) : "loading…"}
        </pre>
      </Panel>

      <Panel title={`${agent.name} — Log`} scroll>
        {agent.recent_logs.length > 0 ? (
          <div className="logs">
            {agent.recent_logs.map((line, index) => (
              <div className="log-line" key={`${line.timestamp}-${index}`}>
                <span className="log-time">{time(line.timestamp)}</span>
                <span className={`log-level ${line.level}`}>{line.level}</span>
                <span style={{ whiteSpace: "normal" }}>{line.message}</span>
              </div>
            ))}
          </div>
        ) : (
          <Empty>No log lines yet.</Empty>
        )}
      </Panel>
    </div>
  );
}
