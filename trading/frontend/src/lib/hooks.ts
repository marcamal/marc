/**
 * Data-loading hooks: polling, and the live event socket.
 *
 * Polling rather than a state library: the dashboard reads a handful of
 * endpoints and the backend is on localhost, so a small `usePoll` is less
 * machinery than React Query would be for the same result. The websocket
 * carries the events that matter immediately (fills, risk vetoes, kill
 * switch), so the poll interval only governs how fresh the numbers are.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { BusEvent, SocketMessage, SystemStatus } from "./types";

export interface PollState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Re-fetch now, without waiting for the next interval. */
  refresh: () => void;
}

/**
 * Fetch on mount, then every `intervalMs`.
 *
 * `deps` controls when the fetcher itself is considered new; pass a stable
 * array, as with `useEffect`.
 */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs = 5000,
  deps: unknown[] = [],
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  // Keep the latest fetcher in a ref so changing it does not restart the
  // interval on every render.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const result = await fetcherRef.current();
        if (cancelled) return;
        setData(result);
        setError(null);
      } catch (cause) {
        if (cancelled) return;
        setError(cause instanceof ApiError ? cause.message : String(cause));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    if (intervalMs <= 0) return () => { cancelled = true; };

    const timer = window.setInterval(load, intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, tick, ...deps]);

  return { data, error, loading, refresh };
}

export interface SocketState {
  connected: boolean;
  /** Most recent events, newest first. */
  events: BusEvent[];
  /** The status snapshot the server sends on connect. */
  snapshot: SystemStatus | null;
  /** Bumped on every event, so pages can cheaply re-poll on activity. */
  version: number;
}

const MAX_EVENTS = 200;

/**
 * Subscribe to the backend's event stream.
 *
 * Reconnects with exponential backoff: a dashboard left open overnight must
 * come back by itself after the backend restarts, rather than showing stale
 * numbers behind a dead socket.
 */
export function useEventSocket(): SocketState {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<BusEvent[]>([]);
  const [snapshot, setSnapshot] = useState<SystemStatus | null>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let delay = 1000;
    let closed = false;

    const connect = () => {
      if (closed) return;

      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/events`);

      socket.onopen = () => {
        setConnected(true);
        delay = 1000; // reset the backoff after a successful connection
      };

      socket.onmessage = (raw) => {
        try {
          const message = JSON.parse(raw.data as string) as SocketMessage;
          if (message.type === "snapshot") {
            setSnapshot(message.data);
          } else {
            setEvents((previous) => [message.data, ...previous].slice(0, MAX_EVENTS));
            setVersion((value) => value + 1);
          }
        } catch {
          // A malformed frame must not kill the socket.
        }
      };

      socket.onclose = () => {
        setConnected(false);
        if (closed) return;
        reconnectTimer = window.setTimeout(connect, delay);
        delay = Math.min(delay * 2, 30000);
      };

      socket.onerror = () => {
        // onclose always follows, which is where reconnection is handled.
        socket?.close();
      };
    };

    connect();

    return () => {
      closed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, []);

  return { connected, events, snapshot, version };
}

/** Run an async action, tracking pending state and surfacing errors. */
export function useAction(): {
  run: (action: () => Promise<unknown>) => Promise<void>;
  pending: boolean;
  error: string | null;
  clearError: () => void;
} {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async (action: () => Promise<unknown>) => {
    setPending(true);
    setError(null);
    try {
      await action();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setPending(false);
    }
  }, []);

  return { run, pending, error, clearError: () => setError(null) };
}
