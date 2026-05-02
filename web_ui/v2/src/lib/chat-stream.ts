/**
 * Shared chat-stream helper.  Every mode follows the same shape:
 *
 *   POST /start  →  { session_id }
 *   GET  /{sid}/events  →  SSE
 *   POST /{sid}/cancel
 *
 * with slight per-mode differences in the event vocabulary.
 * ``createChatStream`` returns a thin ``{ start, cancel, status,
 * sessionId }`` API plus the turn list as a Solid signal so a route
 * component can be ~30 lines instead of 200.
 *
 * Per-mode event handlers customise WHICH events render content +
 * WHICH map to phase tags.  See routes/Research, routes/Thinking,
 * routes/Sentinel for examples.
 */

import { createSignal } from "solid-js";
import type { Accessor } from "solid-js";
import { api, type ApiError } from "./api";
import {
  openEventStream,
  type OpenedStream,
  type StreamStatus,
  type SseEvent,
} from "./sse";
import type { ChatTurn } from "./types";

let _idCounter = 0;
const newId = (): string => `t-${Date.now()}-${++_idCounter}`;

/** Event handler decides what to do with each SSE event.
 *  Return a ``{ append, tag, done, error, replace }`` patch. */
export interface StreamPatch {
  /** Text chunk to append to the assistant's current turn buffer. */
  append?: string;
  /** Replace the assistant turn body wholesale (e.g. final report). */
  replace?: string;
  /** Update the assistant turn's tag (typically the active phase). */
  tag?: string;
  /** Mark the stream complete on this event. */
  done?: boolean;
  /** Mark the stream errored on this event. */
  error?: string;
}

export type EventReducer = (ev: SseEvent) => StreamPatch | null;

export interface ChatStreamConfig<StartReq> {
  /** Path for the POST /start request — e.g. "/api/local-ai/research". */
  startPath: string;
  /** Builds the request body from the user prompt. */
  buildStartBody: (prompt: string) => StartReq;
  /** Returns the events URL given a session id. */
  eventsPath: (sid: string) => string;
  /** Returns the cancel URL given a session id.  ``null`` disables. */
  cancelPath: (sid: string) => string | null;
  /** Per-mode event reducer. */
  reduce: EventReducer;
  /** Optional: kick this when a stream successfully opens (used by
   *  Build to update phase state). */
  onEvent?: (ev: SseEvent) => void;
}

export interface ChatStreamApi {
  turns: Accessor<ChatTurn[]>;
  busy: Accessor<boolean>;
  status: Accessor<StreamStatus>;
  sessionId: Accessor<string | null>;
  start: (prompt: string) => Promise<void>;
  cancel: () => Promise<void>;
  /** Manually append a system / tool turn (e.g. "phase X complete"). */
  pushTurn: (turn: Omit<ChatTurn, "id">) => void;
}

interface StartResp {
  session_id: string;
}

/**
 * Cache of stream singletons keyed by ``startPath`` — every mode has
 * exactly one persistent stream that survives route remounts.  When
 * the user navigates from /build → /system → /build the second mount
 * gets the SAME signals back (turns + busy + status), so an
 * in-flight pipeline keeps streaming and the prior turns stay
 * visible.  Logout clears the cache via ``resetAllChatStreams``.
 */
const _streamCache = new Map<string, ChatStreamApi>();

export function getChatStream<StartReq>(
  cfg: ChatStreamConfig<StartReq>,
): ChatStreamApi {
  const cached = _streamCache.get(cfg.startPath);
  if (cached) return cached;
  const fresh = createChatStream(cfg);
  _streamCache.set(cfg.startPath, fresh);
  return fresh;
}

/** Clear every cached stream — call on logout so the next user
 *  doesn't see the previous one's turns. */
export function resetAllChatStreams(): void {
  _streamCache.clear();
}

export function createChatStream<StartReq>(
  cfg: ChatStreamConfig<StartReq>,
): ChatStreamApi {
  const [turns, setTurns] = createSignal<ChatTurn[]>([]);
  const [busy, setBusy] = createSignal(false);
  const [status, setStatus] = createSignal<StreamStatus>("closed");
  const [sessionId, setSessionId] = createSignal<string | null>(null);

  let stream: OpenedStream | null = null;
  let assistantTurnId: string | null = null;
  let buffer = "";

  const cleanup = (): void => {
    if (stream) {
      stream.close();
      stream = null;
    }
  };
  // No ``onCleanup`` here — module-scoped streams must outlive the
  // route component that mounted them.  Cleanup happens explicitly
  // via ``cancel()`` or ``resetAllChatStreams()`` on logout.

  const patchAssistant = (
    content: string,
    tag?: string,
    streaming = false,
  ): void => {
    if (!assistantTurnId) return;
    const id = assistantTurnId;
    setTurns((prev) =>
      prev.map((t) =>
        t.id === id ? { ...t, content, tag, streaming } : t,
      ),
    );
  };

  const handleEvent = (ev: SseEvent): void => {
    if (cfg.onEvent) cfg.onEvent(ev);
    const patch = cfg.reduce(ev);
    if (!patch) return;

    if (patch.append) {
      buffer += patch.append;
      patchAssistant(buffer, patch.tag, true);
    } else if (patch.replace !== undefined) {
      buffer = patch.replace;
      patchAssistant(buffer, patch.tag, !patch.done && !patch.error);
    } else if (patch.tag !== undefined) {
      patchAssistant(buffer, patch.tag, !patch.done && !patch.error);
    }

    if (patch.error !== undefined) {
      patchAssistant(buffer + `\n\n**Error:** ${patch.error}`, "failed");
      cleanup();
      setBusy(false);
    } else if (patch.done) {
      patchAssistant(buffer || "_(done)_", patch.tag ?? "done");
      cleanup();
      setBusy(false);
    }
  };

  const start = async (prompt: string): Promise<void> => {
    cleanup();
    buffer = "";
    setBusy(true);
    setStatus("connecting");
    setTurns((prev) => [
      ...prev,
      { id: newId(), role: "user", content: prompt, ts: Date.now() },
    ]);
    assistantTurnId = newId();
    setTurns((prev) => [
      ...prev,
      {
        id: assistantTurnId!,
        role: "assistant",
        content: "_(starting…)_",
        streaming: true,
        tag: "starting",
        ts: Date.now(),
      },
    ]);

    try {
      const resp = await api.post<StartResp>(
        cfg.startPath,
        cfg.buildStartBody(prompt),
      );
      setSessionId(resp.session_id);
      stream = openEventStream({
        url: cfg.eventsPath(resp.session_id),
        onStatusChange: (s) => setStatus(s),
        onEvent: handleEvent,
      });
    } catch (err: unknown) {
      const apiErr = err as ApiError | undefined;
      const detail =
        (apiErr?.body as { detail?: string } | undefined)?.detail ??
        (err instanceof Error ? err.message : "Failed to start");
      setBusy(false);
      setStatus("closed");
      patchAssistant(`**Error:** ${String(detail)}`, "failed");
    }
  };

  const cancel = async (): Promise<void> => {
    const sid = sessionId();
    const path = sid ? cfg.cancelPath(sid) : null;
    if (path) {
      try {
        await api.post(path);
      } catch {
        // best-effort
      }
    }
    cleanup();
    patchAssistant(buffer + "\n\n_(cancelled)_", "cancelled");
    setBusy(false);
  };

  const pushTurn = (turn: Omit<ChatTurn, "id">): void => {
    setTurns((prev) => [...prev, { ...turn, id: newId() }]);
  };

  return {
    turns,
    busy,
    status,
    sessionId,
    start,
    cancel,
    pushTurn,
  };
}

/**
 * Default reducer for "simple" modes that mostly stream text +
 * report a few phase markers.  Covers Research, Thinking,
 * Consortium, Sentinel.  Build has its own richer reducer in
 * routes/Build.tsx because it has 9 phase-specific event types.
 */
export const SIMPLE_TEXT_REDUCER: EventReducer = (ev) => {
  const type = String(ev.type ?? "");

  // Phase markers — update tag, no content.
  if (type.endsWith("_phase_start") || type === "phase_start") {
    const phase = String(ev.phase ?? ev.label ?? "");
    return phase ? { tag: `phase: ${phase}` } : null;
  }
  if (type.endsWith("_phase_complete") || type === "phase_complete") {
    return null; // phases auto-advance via the next phase_start
  }

  // Snapshot — many backends send a full state dict at stream open.
  if (type.endsWith("_snapshot") || type === "snapshot") {
    return null;
  }

  // Streaming text — the backends use various keys.
  if (
    type === "chunk" ||
    type === "delta" ||
    type === "research_chunk" ||
    type === "synthesis_chunk" ||
    type === "thinking_chunk"
  ) {
    const text =
      typeof ev.text === "string"
        ? ev.text
        : typeof ev.content === "string"
          ? ev.content
          : "";
    return text ? { append: text } : null;
  }

  // Final deliverable — replace buffer with full report.
  if (
    type === "deliverable_ready" ||
    type === "research_complete" ||
    type === "thinking_complete" ||
    type === "consortium_completed" ||
    type === "sentinel_completed" ||
    type === "complete"
  ) {
    const final =
      typeof ev.markdown === "string"
        ? ev.markdown
        : typeof ev.report === "string"
          ? ev.report
          : typeof ev.content === "string"
            ? ev.content
            : typeof ev.summary === "string"
              ? ev.summary
              : "";
    return final
      ? { replace: final, done: true, tag: "done" }
      : { done: true, tag: "done" };
  }

  if (type === "done") {
    return { done: true, tag: "done" };
  }

  // Errors.
  if (type.endsWith("_error") || type === "error") {
    const msg = String(ev.message ?? ev.error ?? "stream error");
    return { error: msg };
  }

  // Cancellations.
  if (type.endsWith("_cancelled") || type === "cancelled") {
    return { tag: "cancelled", done: true };
  }

  // Unknown — pass through any text payload.
  if (typeof ev.text === "string") {
    return { append: ev.text };
  }
  return null;
};
