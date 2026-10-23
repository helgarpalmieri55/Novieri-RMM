// Live status: WebSocket to /api/v1/ws, events published by gateway/API.

import { useEffect, useRef } from "react";
import { accessToken } from "./auth";

export interface UiEvent {
  kind: string;
  job_id?: string;
  state?: string;
  [key: string]: unknown;
}

export function useLiveEvents(onEvent: (event: UiEvent) => void): void {
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let closed = false;
    let retryMs = 1000;

    const connect = () => {
      const token = accessToken();
      if (!token || closed) return;
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      // The token goes in the subprotocol list, NEVER in the URL: a query
      // string is written verbatim to the server's access log, and no
      // logging-side redaction can reach it. The server requires exactly
      // ["bearer", token] and echoes back "bearer".
      socket = new WebSocket(`${scheme}://${window.location.host}/api/v1/ws`, ["bearer", token]);
      socket.onmessage = (message) => {
        try {
          handler.current(JSON.parse(message.data as string) as UiEvent);
        } catch {
          // ignore malformed frames
        }
      };
      socket.onopen = () => {
        retryMs = 1000;
      };
      socket.onclose = () => {
        if (!closed) {
          retryMs = Math.min(retryMs * 2, 30000);
          setTimeout(connect, retryMs);
        }
      };
    };
    connect();
    return () => {
      closed = true;
      socket?.close();
    };
  }, []);
}
