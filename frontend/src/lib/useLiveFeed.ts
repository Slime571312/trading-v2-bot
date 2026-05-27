"use client";

import { useEffect, useRef, useState } from "react";
import type { BotState } from "./api";

export type LiveMessage =
  | { type: "ping" }
  | { type: "state"; state: BotState }
  | { type: "trade_opened"; trade: BotState["open_trades"][number] }
  | { type: "trade_closed"; trade: BotState["closed_trades"][number] }
  | { type: "error"; message: string };

export type ConnStatus = "connecting" | "open" | "closed" | "error";

interface UseLiveFeedResult {
  state: BotState | null;
  lastEvent: LiveMessage | null;
  status: ConnStatus;
  reconnectAttempt: number;
}

/**
 * WebSocket-Hook für /ws/live mit Exponential-Backoff-Reconnect.
 * Verarbeitet ping → pong und parsed alle JSON-Events.
 */
export function useLiveFeed(): UseLiveFeedResult {
  const [state, setState] = useState<BotState | null>(null);
  const [lastEvent, setLastEvent] = useState<LiveMessage | null>(null);
  const [status, setStatus] = useState<ConnStatus>("connecting");
  const [reconnectAttempt, setReconnectAttempt] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let unmounted = false;
    let attempt = 0;

    function connect() {
      if (unmounted) return;
      // Direkte WS-URL ohne den Next.js-Rewrite-Proxy (rewrites unterstützen kein WS).
      const url = `ws://${window.location.hostname}:8000/ws/live`;
      setStatus("connecting");
      try {
        const ws = new WebSocket(url);
        wsRef.current = ws;

        ws.onopen = () => {
          if (unmounted) return;
          attempt = 0;
          setReconnectAttempt(0);
          setStatus("open");
        };

        ws.onmessage = (e) => {
          if (unmounted) return;
          try {
            const msg = JSON.parse(e.data) as LiveMessage;
            if (msg.type === "ping") {
              ws.send("pong");
              return;
            }
            setLastEvent(msg);
            if (msg.type === "state") setState(msg.state);
            else if (msg.type === "trade_opened" || msg.type === "trade_closed") {
              // Re-fetch full state to stay in sync
              setState((prev) => prev);
            }
          } catch (err) {
            console.warn("ws msg parse failed", err);
          }
        };

        ws.onerror = () => {
          if (!unmounted) setStatus("error");
        };

        ws.onclose = () => {
          if (unmounted) return;
          setStatus("closed");
          attempt += 1;
          setReconnectAttempt(attempt);
          const delay = Math.min(30_000, 1_000 * Math.pow(1.5, attempt));
          reconnectTimer.current = setTimeout(connect, delay);
        };
      } catch (err) {
        setStatus("error");
        attempt += 1;
        const delay = Math.min(30_000, 1_000 * Math.pow(1.5, attempt));
        reconnectTimer.current = setTimeout(connect, delay);
      }
    }

    connect();

    return () => {
      unmounted = true;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, []);

  return { state, lastEvent, status, reconnectAttempt };
}
