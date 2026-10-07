/**
 * Typed client for the ATLAS backend.
 *
 * Architecture rule, enforced by this file being the only place that does
 * network IO:
 *
 *     Browser -> ATLAS backend -> Alpaca
 *
 * The frontend never holds Alpaca credentials and never calls Alpaca. In
 * development, Vite proxies /api and /ws to 127.0.0.1:8000.
 */

import type {
  Account,
  AgentDescriptor,
  AgentGraphEdge,
  AgentGraphNode,
  BrokerCapabilities,
  BusEvent,
  JournalEntry,
  KillSwitchStatus,
  LogRecord,
  MentorExplanation,
  MentorExplanation as Explanation,
  Order,
  PortfolioSnapshot,
  Position,
  RiskCheck,
  RiskDecision,
  ScannerResult,
  StrategyInfo,
  SystemStatus,
  WatchlistRow,
} from "./types";

const BASE = "/api";

/** An API error carrying the backend's own explanation. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch (cause) {
    // Almost always "the backend is not running". Say that, rather than
    // surfacing a bare "Failed to fetch".
    throw new ApiError(
      "Cannot reach the ATLAS backend. Is it running on http://127.0.0.1:8000?",
      0,
      String(cause),
    );
  }

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail;
    } catch {
      detail = await response.text().catch(() => undefined);
    }
    throw new ApiError(detail || `${response.status} ${response.statusText}`, response.status, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const api = {
  // --- system ------------------------------------------------------------
  status: () => request<SystemStatus>("/system/status"),
  health: () =>
    request<{ status: string; components: Record<string, boolean> }>("/system/health"),
  mode: () =>
    request<{
      effective: string;
      is_live: boolean;
      is_simulated: boolean;
      live_gates: Record<string, boolean>;
      warnings: string[];
      explanation: string;
    }>("/system/mode"),
  capabilities: () =>
    request<{ capabilities: BrokerCapabilities; unsupported: string[]; notes: string[] }>(
      "/system/capabilities",
    ),
  logs: (limit = 100, level?: string) =>
    request<{ logs: LogRecord[] }>(
      `/system/logs?limit=${limit}${level ? `&level=${level}` : ""}`,
    ),
  events: (limit = 100, pattern = "*") =>
    request<{ events: BusEvent[] }>(
      `/system/events?limit=${limit}&pattern=${encodeURIComponent(pattern)}`,
    ),

  // --- kill switch -------------------------------------------------------
  killSwitch: () => request<KillSwitchStatus>("/system/kill-switch"),
  engageKillSwitch: (reason: string) =>
    post<{ engaged: boolean; orders_canceled: number; positions_flattened: number }>(
      "/system/kill-switch/engage",
      { reason },
    ),
  releaseKillSwitch: () =>
    post<{ engaged: boolean; status: KillSwitchStatus }>("/system/kill-switch/release"),

  // --- account -----------------------------------------------------------
  account: () => request<{ account: Account; simulated: boolean }>("/account"),
  positions: () =>
    request<{ positions: Position[]; count: number; total_exposure: number }>("/positions"),
  orders: (status: "open" | "closed" | "all" = "all", limit = 50) =>
    request<{ orders: Order[]; count: number }>(`/orders?status=${status}&limit=${limit}`),
  portfolio: () =>
    request<{ portfolio: PortfolioSnapshot; source: string }>("/portfolio"),
  equityCurve: (limit = 500) =>
    request<{ points: Array<{ created_at: string; equity: number }> }>(
      `/portfolio/equity-curve?limit=${limit}`,
    ),

  // --- market ------------------------------------------------------------
  clock: () =>
    request<{
      clock: { is_open: boolean; next_open: string | null; next_close: string | null };
      status: string;
      seconds_until_close: number | null;
    }>("/market/clock"),
  watchlist: () =>
    request<{ universe: string; symbols: string[]; rows: WatchlistRow[] }>(
      "/market/watchlist",
    ),
  bars: (symbol: string, timeframe = "5Min", limit = 100) =>
    request<{
      symbol: string;
      bars: Array<{ timestamp: string; open: number; high: number; low: number; close: number; volume: number }>;
    }>(`/market/bars/${symbol}?timeframe=${timeframe}&limit=${limit}`),
  quote: (symbol: string) =>
    request<{ quote: { bid_price: number; ask_price: number; mid: number }; age_seconds: number; stale: boolean }>(
      `/market/quote/${symbol}`,
    ),

  // --- agents ------------------------------------------------------------
  agents: (logLimit = 5) =>
    request<{ agents: AgentDescriptor[]; health: Record<string, unknown> }>(
      `/agents?log_limit=${logLimit}`,
    ),
  agent: (id: string) =>
    request<{ agent: AgentDescriptor; detail: Record<string, unknown> }>(`/agents/${id}`),
  agentGraph: () =>
    request<{ nodes: AgentGraphNode[]; edges: AgentGraphEdge[] }>("/agents/graph"),
  startAgent: (id: string) => post<{ agent: AgentDescriptor }>(`/agents/${id}/start`),
  stopAgent: (id: string) => post<{ agent: AgentDescriptor }>(`/agents/${id}/stop`),
  restartAgent: (id: string) => post<{ agent: AgentDescriptor }>(`/agents/${id}/restart`),
  briefing: () => request<Record<string, unknown>>("/agents/briefing/summary"),

  // --- scanner -----------------------------------------------------------
  scannerResults: (limit = 20) =>
    request<{
      universe: string;
      universe_size: number;
      results: ScannerResult[];
      excluded: Record<string, string[]>;
      agent_status: string;
      disclaimer: string;
    }>(`/scanner/results?limit=${limit}`),
  runScan: () =>
    post<{ scanned: number; ranked: number; results: ScannerResult[] }>("/scanner/run"),
  universes: () =>
    request<{
      active: string;
      universes: Record<string, string[]>;
      sizes: Record<string, number>;
      streaming_limit: number;
      note: string;
    }>("/scanner/universes"),

  // --- strategies --------------------------------------------------------
  strategies: () =>
    request<{
      globally_enabled: boolean;
      loaded: number;
      active: number;
      strategies: StrategyInfo[];
      warning: string;
    }>("/strategies"),
  enableStrategy: (id: string) =>
    post<{ strategy: StrategyInfo; notes: string[]; persisted: boolean }>(
      `/strategies/${id}/enable`,
    ),
  disableStrategy: (id: string) =>
    post<{ strategy: StrategyInfo }>(`/strategies/${id}/disable`),

  // --- risk --------------------------------------------------------------
  riskStatus: () =>
    request<{
      config: Record<string, number | boolean | string>;
      agent: Record<string, unknown>;
      kill_switch: KillSwitchStatus;
      hard_ceilings: Record<string, number>;
    }>("/risk/status"),
  riskDecisions: (limit = 25) =>
    request<{ decisions: RiskDecision[] }>(`/risk/decisions?limit=${limit}`),
  riskEvents: (limit = 25) =>
    request<{ events: Array<Record<string, unknown>> }>(`/risk/events?limit=${limit}`),
  /** Read-only: evaluates a hypothetical trade. Never places an order. */
  checkTrade: (payload: {
    symbol: string;
    side: "buy" | "sell";
    quantity: number;
    entry_price: number;
    stop_price: number;
    target_price?: number | null;
  }) =>
    post<{
      proposal: Record<string, unknown>;
      decision: RiskDecision & { checks: RiskCheck[] };
      summary: string;
      note: string;
    }>("/risk/check", payload),

  // --- journal / mentor --------------------------------------------------
  journal: (limit = 25, search?: string) =>
    request<{ entries: JournalEntry[] }>(
      `/journal?limit=${limit}${search ? `&search=${encodeURIComponent(search)}` : ""}`,
    ),
  explanations: (limit = 20) =>
    request<{ explanations: Explanation[] }>(`/mentor/explanations?limit=${limit}`),
  trace: (traceId: string) =>
    request<{ trace_id: string; timeline: Array<Record<string, unknown>>; count: number }>(
      `/trace/${traceId}`,
    ),
};

export type { MentorExplanation };
