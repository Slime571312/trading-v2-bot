"use client";

import { useEffect, useRef, useState } from "react";
import type { BotInstance } from "./api";

export type LiveMessage =
  | { type: "ping" }
  | { type: "snapshot"; instances: Record<string, BotInstance> }
  | { type: "instance_update"; instrument: string; instance: BotInstance }
  | { type: "trade_opened"; instrument: string; trade: BotInstance["open_trades"][number] }
  | { type: "trade_closed"; instrument: string; trade: BotInstance["closed_trades"][number] }
  | { type: "error"; instrument?: string; message: string };

export type ConnStatus = "connecting" | "open" | "closed" | "error";

interface UseLiveFeedResult {
  instances: Record<string, BotInstance>;
  lastEvent: LiveMessage | null;
  status: ConnStatus;
  reconnectAttempt: number;
}

/**
 * Multi-Bot-WS-Hook: hält das aktuelle Instances-Mapping, das vom Server
 * gepusht wird. Initial-Snapshot beim Connect, dann incremental updates.
 */
export function useLiveFeed(): UseLiveFeedResult {
  const [instances, setInstances] = useState<Record<string, BotInstance>>({});
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
            if (msg.type === "ping") { ws.send("pong"); return; }

            setLastEvent(msg);

            if (msg.type === "snapshot") {
              setInstances(msg.instances);
            } else if (msg.type === "instance_update") {
              setInstances((prev) => ({ ...prev, [msg.instrument]: msg.instance }));
            } else if (msg.type === "trade_opened" || msg.type === "trade_closed") {
              // Instance-Update kommt separat; nichts zu tun hier
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
      } catch {
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

  return { instances, lastEvent, status, reconnectAttempt };
}
