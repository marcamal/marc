/**
 * The command-center header: status lights, the mode badge, and the kill
 * switch.
 *
 * The mode badge is the most important element on the page. PAPER and
 * SIMULATED are calm; LIVE is red and pulses, because mistaking a live
 * session for a paper one is the expensive mistake this whole system is
 * designed to prevent.
 */

import { useState } from "react";
import { api } from "../lib/api";
import { useAction } from "../lib/hooks";
import type { SystemStatus } from "../lib/types";
import { Indicator } from "./common";

type DotState = "ok" | "active" | "warn" | "error" | "off";

function brokerState(status: SystemStatus | null): DotState {
  if (!status) return "off";
  if (!status.broker.has_credentials) return "warn";
  return "ok";
}

function dataState(status: SystemStatus | null): DotState {
  if (!status) return "off";
  const states = Object.values(status.market_data.connection_states ?? {});
  if (states.includes("connected")) return "active";
  if (states.includes("reconnecting") || states.includes("connecting")) return "warn";
  if (states.includes("error")) return "error";
  return "off";
}

function agentState(status: SystemStatus | null): DotState {
  if (!status) return "off";
  const { running = 0, unhealthy = 0 } = status.agents;
  if (unhealthy > 0) return "warn";
  return running > 0 ? "ok" : "off";
}

function riskState(status: SystemStatus | null): DotState {
  if (!status) return "off";
  return status.kill_switch.engaged ? "error" : "ok";
}

export function Header({
  status,
  socketConnected,
  onChanged,
}: {
  status: SystemStatus | null;
  socketConnected: boolean;
  onChanged: () => void;
}) {
  const { run, pending } = useAction();
  const [confirming, setConfirming] = useState(false);

  const engaged = status?.kill_switch.engaged ?? false;
  const isLive = status?.mode.is_live ?? false;
  const simulated = status?.broker.simulated ?? false;

  const modeLabel = isLive
    ? "● LIVE TRADING"
    : simulated
      ? "SIMULATED"
      : `${(status?.mode.effective ?? "paper").toUpperCase()} MODE`;

  const modeClass = isLive ? "live" : simulated ? "simulated" : "paper";

  const toggleKillSwitch = async () => {
    if (engaged) {
      await run(async () => {
        await api.releaseKillSwitch();
        onChanged();
      });
      return;
    }
    if (!confirming) {
      setConfirming(true);
      return;
    }
    await run(async () => {
      await api.engageKillSwitch("engaged from the dashboard");
      onChanged();
    });
    setConfirming(false);
  };

  return (
    <header className="header">
      <span className="brand">
        ATLAS
        <span className="version">v{status?.version ?? "—"}</span>
      </span>

      <span className={`mode-badge ${modeClass}`}>{modeLabel}</span>

      <div className="indicators">
        <Indicator
          label="Alpaca"
          state={brokerState(status)}
          title={
            status?.broker.has_credentials
              ? `${status.broker.name} (${status.broker.endpoint})`
              : "No API credentials — running on the simulator"
          }
        />
        <Indicator
          label="Market"
          state={status?.market_clock?.is_open ? "ok" : "off"}
          title={status?.market_clock?.is_open ? "Market open" : "Market closed"}
        />
        <Indicator
          label="Data"
          state={dataState(status)}
          title={`Feed: ${status?.market_data.feed ?? "—"}`}
        />
        <Indicator
          label="Agents"
          state={agentState(status)}
          title={`${status?.agents.running ?? 0}/${status?.agents.registered ?? 0} running`}
        />
        <Indicator
          label="Risk"
          state={riskState(status)}
          title={engaged ? "KILL SWITCH ENGAGED" : "Risk controls active"}
        />
        <Indicator
          label="Stream"
          state={socketConnected ? "ok" : "off"}
          title={socketConnected ? "Dashboard socket connected" : "Dashboard socket down"}
        />
      </div>

      <span className="spacer" />

      <div className="killswitch">
        {confirming && !engaged ? (
          <>
            <span className="mono" style={{ fontSize: 11, color: "var(--warn)" }}>
              Stop all trading?
            </span>
            <button className="btn sm" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </>
        ) : null}
        <button
          className={`btn ${engaged ? "primary" : "danger"}`}
          onClick={toggleKillSwitch}
          disabled={pending}
          title={
            engaged
              ? "Release the kill switch and allow trading again"
              : "Emergency stop: block all new orders and cancel resting orders"
          }
        >
          {engaged ? "Release Kill Switch" : confirming ? "Confirm Stop" : "Kill Switch"}
        </button>
      </div>
    </header>
  );
}
