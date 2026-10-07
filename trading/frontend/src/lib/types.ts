/**
 * Types mirroring the backend's Pydantic models.
 *
 * Hand-written rather than generated, so the dashboard declares exactly the
 * fields it uses. If a field disappears from the API, TypeScript complains at
 * the place that reads it.
 */

export type TradingMode = "paper" | "live" | "backtest" | "replay";

export type AgentStatusValue =
  | "created"
  | "starting"
  | "idle"
  | "working"
  | "stopping"
  | "stopped"
  | "error"
  | "unhealthy";

export type ConnectionStateValue =
  | "disconnected"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "degraded"
  | "error";

export interface LiveGates {
  mode_is_live: boolean;
  live_trading_enabled: boolean;
  manual_confirmation: boolean;
}

export interface BrokerCapabilities {
  broker_name: string;
  detected: boolean;
  supports_crypto: boolean;
  supports_options: boolean;
  supports_direct_bonds: boolean;
  supports_fractional_shares: boolean;
  supports_short_selling: boolean;
  margin_enabled: boolean;
  max_leverage: number;
  data_feed: string;
  max_stream_symbols: number;
  has_paid_data_plan: boolean;
  notes: string[];
}

export interface KillSwitchStatus {
  engaged: boolean;
  engaged_in_memory: boolean;
  file_present: boolean;
  file_path: string;
  reason: string | null;
  triggered_by: string | null;
  engaged_at: string | null;
  will_cancel_orders: boolean;
  will_flatten_positions: boolean;
}

export interface SystemStatus {
  version: string;
  started_at: string | null;
  uptime_seconds: number;
  mode: {
    effective: TradingMode;
    requested: TradingMode;
    is_live: boolean;
    live_gates: LiveGates;
    warnings: string[];
  };
  broker: {
    name: string;
    simulated: boolean;
    has_credentials: boolean;
    endpoint: string;
    capabilities: BrokerCapabilities | null;
  };
  market_data: {
    started?: boolean;
    feed: string;
    symbol_count?: number;
    seconds_since_last_message?: number | null;
    connection_states?: Record<string, string>;
    streams?: Record<string, Record<string, unknown>>;
  };
  market_clock: {
    is_open: boolean;
    next_open: string | null;
    next_close: string | null;
  } | null;
  kill_switch: KillSwitchStatus;
  agents: {
    registered?: number;
    running?: number;
    unhealthy?: number;
    unhealthy_ids?: string[];
    all_healthy?: boolean;
  };
  strategies: {
    globally_enabled: boolean;
    loaded: number;
    active: number;
    strategies: StrategyInfo[];
  };
  orders: {
    trading_day: string;
    orders_today: number;
    open_orders: number;
  };
  database: {
    url: string;
    size_bytes: number | null;
    recorder: { rows_written: number; write_errors: number };
  };
  event_bus: {
    running: boolean;
    published_total: number;
    total_dropped: number;
  };
  startup_errors: string[];
}

export interface Account {
  account_id: string;
  currency: string;
  equity: number;
  last_equity: number;
  cash: number;
  buying_power: number;
  daily_pl: number;
  daily_pl_pct: number;
  daytrade_count: number;
  pattern_day_trader: boolean;
  trading_blocked: boolean;
  is_restricted: boolean;
}

export interface Position {
  symbol: string;
  quantity: number;
  side: "long" | "short";
  average_entry_price: number;
  current_price: number | null;
  market_value: number;
  unrealized_pl: number;
  unrealized_pl_pct: number;
  exposure: number;
  strategy_id: string | null;
}

export interface Order {
  id: string;
  client_order_id: string;
  symbol: string;
  side: "buy" | "sell";
  order_type: string;
  status: string;
  quantity: number;
  filled_quantity: number;
  average_fill_price: number | null;
  submitted_at: string | null;
  strategy_id: string | null;
  is_open: boolean;
}

export interface PortfolioSnapshot {
  as_of: string;
  equity: number;
  cash: number;
  buying_power: number;
  positions: Position[];
  total_exposure: number;
  total_exposure_pct: number;
  unrealized_pl: number;
  daily_pl: number;
  daily_pl_pct: number;
  high_water_mark: number;
  drawdown_pct: number;
  open_position_count: number;
  exposure_by_correlation_group: Record<string, number>;
  reconciliation_mismatch: boolean;
  mismatch_detail: string[];
}

export interface AgentStats {
  cycles_completed: number;
  events_handled: number;
  messages_published: number;
  errors: number;
  last_error: string | null;
  last_cycle_duration_ms: number | null;
  uptime_seconds: number;
}

export interface AgentLogLine {
  timestamp: string;
  level: string;
  message: string;
  trace_id: string | null;
}

export interface AgentDescriptor {
  id: string;
  name: string;
  role: string;
  type: string;
  status: AgentStatusValue;
  current_task: string | null;
  last_activity: string | null;
  confidence: number | null;
  inputs: string[];
  outputs: string[];
  subscriptions: string[];
  stats: AgentStats;
  recent_logs: AgentLogLine[];
  autostart: boolean;
  is_healthy: boolean;
}

export interface AgentGraphNode {
  id: string;
  name: string;
  type: string;
  status: AgentStatusValue;
}

export interface AgentGraphEdge {
  source: string;
  target: string;
  label: string;
}

export interface ScannerResult {
  symbol: string;
  rank: number;
  score: number;
  price: number | null;
  percent_change: number | null;
  volume: number | null;
  relative_volume: number | null;
  atr_percent: number | null;
  spread_percent: number | null;
  distance_from_vwap_pct: number | null;
  rsi: number | null;
  momentum_pct: number | null;
  ema_stack_bullish: boolean | null;
  score_breakdown: Record<string, number>;
  notes: string[];
}

export interface StrategyInfo {
  id: string;
  name: string;
  enabled: boolean;
  symbols: string[];
  timeframe: string;
  params: Record<string, unknown>;
  proposals_generated: number;
  last_run_at: string | null;
  last_error: string | null;
  description: string;
}

export interface RiskCheck {
  rule: string;
  passed: boolean;
  detail: string;
  limit: number | null;
  observed: number | null;
}

export interface RiskDecision {
  proposal_id: string;
  decision: "approved" | "rejected" | "approved_reduced";
  checks: RiskCheck[];
  approved_quantity: number | null;
  reasons: string[];
  decided_at: string;
  is_approved: boolean;
  failed_rules: string[];
}

export interface WatchlistRow {
  symbol: string;
  price: number | null;
  percent_change: number | null;
  volume: number | null;
}

export interface LogRecord {
  timestamp: string;
  level: string;
  logger: string;
  message: string;
  trace_id: string | null;
  context: Record<string, unknown>;
}

export interface BusEvent {
  id: string;
  topic: string;
  timestamp: string;
  source: string;
  trace_id: string | null;
  [key: string]: unknown;
}

export interface MentorExplanation {
  title: string;
  body: string;
  symbol: string | null;
  category: string;
  lessons: string[];
  timestamp: string;
}

export interface JournalEntry {
  id: number;
  entry_type: string;
  symbol: string | null;
  title: string;
  body: string;
  lessons: string[];
  outcome: string | null;
  created_at: string;
}

/** A live event pushed over the dashboard websocket. */
export type SocketMessage =
  | { type: "snapshot"; data: SystemStatus }
  | { type: "event"; topic: string; data: BusEvent };
