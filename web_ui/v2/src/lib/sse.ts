/**
 * SSE wrapper — `EventSource` with the three things vanilla
 * EventSource lacks:
 *
 *   1. ``event_id``-based dedup over a 200 ms window so the same
 *      event redelivered via Redis Pub/Sub fan-out doesn't render
 *      twice.
 *   2. Reconnect with exponential backoff + a banner-state callback
 *      so the UI can surface "reconnecting…" / "offline" instead of
 *      freezing silently.
 *   3. Token-aware re-establishment — when the access token rotates
 *      (auth refresh), tear down + reopen so the new cookie set is
 *      sent along with the request.
 *
 * Usage:
 *
 *   const stream = openEventStream({
 *     url: `/api/code/${sid}/events`,
 *     onEvent: (ev) => store.append(ev),
 *     onStatusChange: (s) => bannerStore.set(s),
 *   });
 *   onCleanup(() => stream.close());
 */

import { onAuthChange } from "./api";

export type StreamStatus =
  | "connecting"
  | "open"
  | "reconnecting"
  | "offline"
  | "closed";

export interface SseEvent {
  type: string;
  /** Server-assigned id used for dedup.  Optional — events without
   *  an id always render; only events with the same non-null id
   *  inside the dedup window are filtered. */
  event_id?: string;
  [key: string]: unknown;
}

export interface OpenStreamOptions {
  url: string;
  onEvent: (ev: SseEvent) => void;
  onStatusChange?: (s: StreamStatus) => void;
  /** ms — drop duplicate ``event_id`` values seen within this
   *  window.  Default 200 ms, matching the backend's coalescer. */
  dedupWindowMs?: number;
  /** Reconnect backoff schedule in ms. */
  backoff?: number[];
}

export interface OpenedStream {
  close: () => void;
  status: () => StreamStatus;
}

const DEFAULT_BACKOFF = [500, 1000, 2000, 4000, 8000, 15000];

export function openEventStream(opts: OpenStreamOptions): OpenedStream {
  const dedupWindow = opts.dedupWindowMs ?? 200;
  const backoff = opts.backoff ?? DEFAULT_BACKOFF;

  let es: EventSource | null = null;
  let retries = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;
  let lastStatus: StreamStatus = "connecting";
  let unsubAuth: (() => void) | null = null;

  const recentIds = new Map<string, number>();

  const setStatus = (s: StreamStatus) => {
    if (lastStatus === s) return;
    lastStatus = s;
    if (opts.onStatusChange) opts.onStatusChange(s);
  };

  const cleanupES = () => {
    if (es) {
      try {
        es.close();
      } catch {
        // ignore
      }
      es = null;
    }
  };

  const scheduleReconnect = () => {
    if (closed) return;
    cleanupES();
    setStatus(retries === 0 ? "reconnecting" : "offline");
    const delay = backoff[Math.min(retries, backoff.length - 1)] ?? 15000;
    retries += 1;
    reconnectTimer = setTimeout(connect, delay);
  };

  const connect = () => {
    if (closed) return;
    setStatus(retries === 0 ? "connecting" : "reconnecting");
    cleanupES();
    try {
      es = new EventSource(opts.url, { withCredentials: true });
    } catch (e) {
      console.error("sse: failed to open EventSource", e);
      scheduleReconnect();
      return;
    }
    es.onopen = () => {
      retries = 0;
      setStatus("open");
    };
    es.onerror = () => {
      // The browser auto-retries by default but lacks the banner +
      // backoff curve we want.  Force-close + schedule ourselves.
      scheduleReconnect();
    };
    es.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data) as SseEvent;
        // Dedup by event_id.
        const id = data.event_id;
        if (typeof id === "string" && id.length > 0) {
          const now = Date.now();
          // Garbage-collect old entries on every ~50 inserts so the
          // map doesn't grow unbounded.
          if (recentIds.size > 50) {
            const cutoff = now - dedupWindow;
            for (const [k, t] of recentIds) {
              if (t < cutoff) recentIds.delete(k);
            }
          }
          if (recentIds.has(id)) {
            const seenAt = recentIds.get(id);
            if (typeof seenAt === "number" && now - seenAt < dedupWindow) {
              return; // duplicate within window
            }
          }
          recentIds.set(id, now);
        }
        opts.onEvent(data);
      } catch (err) {
        console.warn("sse: bad event payload", err, evt.data);
      }
    };
  };

  // Re-establish on access-token rotation so the new cookie set is
  // attached to the next handshake.
  unsubAuth = onAuthChange(() => {
    if (closed) return;
    retries = 0;
    connect();
  });

  connect();

  return {
    close: () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      cleanupES();
      if (unsubAuth) unsubAuth();
      setStatus("closed");
    },
    status: () => lastStatus,
  };
}
