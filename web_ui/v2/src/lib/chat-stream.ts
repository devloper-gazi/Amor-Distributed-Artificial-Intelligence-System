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
import { sessions as sessionsApi } from "./sessions";
import { invalidateSessionsList } from "./query-client";
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
  /** Cycle F Sprint 5 — push a fresh turn into the thread (e.g. an
   *  inline approval card on an `approval_required` SSE event).
   *  The chat-stream loop forwards this to the internal `pushTurn`
   *  helper.  When set, append/replace/tag are ignored for this
   *  patch — pushTurn is its own self-contained operation. */
  pushTurn?: Omit<ChatTurn, "id">;
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
  /** Cycle D Sessions polish — when set, every ``start`` first
   *  registers a chat_session row for this mode (so the sidebar
   *  reflects the new session immediately) and bumps its
   *  ``updated_at`` on done / error / cancelled.  Pass the canonical
   *  backend mode string (``"research"`` / ``"thinking"`` /
   *  ``"consortium"`` / ``"sentinel"``).  Omit for modes that don't
   *  belong in the chat-sessions sidebar (e.g. one-shot agent runs). */
  chatSessionMode?: string;
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

/** localStorage key per mode — survives F5 / browser restart so the
 *  user's conversation isn't wiped by an accidental reload.  We only
 *  persist the conversation transcript (``turns``); the live
 *  EventSource is NOT resumed across reloads (the server-side
 *  pipeline session may be terminal or gone). */
const STORAGE_PREFIX = "amor.chat.v1.";
const turnsKey = (startPath: string): string =>
  `${STORAGE_PREFIX}${startPath}.turns`;

function loadPersistedTurns(startPath: string): ChatTurn[] {
  try {
    const raw = localStorage.getItem(turnsKey(startPath));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as ChatTurn[]) : [];
  } catch {
    return [];
  }
}

function persistTurns(startPath: string, turns: ChatTurn[]): void {
  try {
    // Cap history at last 100 turns / 256 KB to avoid blowing up
    // localStorage on a long-running chat.
    const sliced = turns.slice(-100);
    const json = JSON.stringify(sliced);
    if (json.length > 256_000) {
      // Drop oldest until we fit.
      let i = 0;
      while (i < sliced.length - 4) {
        const tail = sliced.slice(i + 1);
        if (JSON.stringify(tail).length <= 256_000) {
          localStorage.setItem(turnsKey(startPath), JSON.stringify(tail));
          return;
        }
        i += 1;
      }
    }
    localStorage.setItem(turnsKey(startPath), json);
  } catch {
    // Quota exceeded / disabled — best-effort.
  }
}

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
  for (const key of _streamCache.keys()) {
    try {
      localStorage.removeItem(`${STORAGE_PREFIX}${key}.turns`);
    } catch {
      // ignore
    }
  }
  _streamCache.clear();
}

export function createChatStream<StartReq>(
  cfg: ChatStreamConfig<StartReq>,
): ChatStreamApi {
  // Hydrate persisted turns so an F5 / browser-restart doesn't lose
  // the user's previous conversation.  Live stream isn't resumed —
  // turns are read-only transcript replay until the next prompt.
  const [turns, setTurns] = createSignal<ChatTurn[]>(
    loadPersistedTurns(cfg.startPath),
  );
  const [busy, setBusy] = createSignal(false);
  const [status, setStatus] = createSignal<StreamStatus>("closed");
  const [sessionId, setSessionId] = createSignal<string | null>(null);

  // Persist on every turns mutation.  ``setTurns`` is wrapped so all
  // mutations flow through one place.
  const persistingSetTurns: typeof setTurns = (next) => {
    const result = setTurns(next as Parameters<typeof setTurns>[0]);
    queueMicrotask(() => persistTurns(cfg.startPath, turns()));
    return result;
  };

  let stream: OpenedStream | null = null;
  let assistantTurnId: string | null = null;
  let buffer = "";
  // Cycle D Sessions polish — chat_sessions.id linked to this stream's
  // current run.  Bumped on done / error / cancelled so the sidebar's
  // "Now" group updates with the most-recent activity.
  let chatSessionId: string | null = null;

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
    persistingSetTurns((prev) =>
      prev.map((t) =>
        t.id === id ? { ...t, content, tag, streaming } : t,
      ),
    );
  };

  const handleEvent = (ev: SseEvent): void => {
    if (cfg.onEvent) cfg.onEvent(ev);
    const patch = cfg.reduce(ev);
    if (!patch) return;

    // Cycle F Sprint 5 — pushTurn is its own self-contained
    // operation: it doesn't touch the assistant buffer at all.
    // Used for inline cards (approval prompt, future tool cards)
    // that live as their own turns in the thread.
    if (patch.pushTurn) {
      persistingSetTurns((prev) => [
        ...prev,
        { ...patch.pushTurn!, id: newId() },
      ]);
    }

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
      bumpChatSession();
    } else if (patch.done) {
      patchAssistant(buffer || "_(done)_", patch.tag ?? "done");
      cleanup();
      setBusy(false);
      bumpChatSession();
    }
  };

  const start = async (prompt: string): Promise<void> => {
    cleanup();
    buffer = "";
    setBusy(true);
    setStatus("connecting");
    persistingSetTurns((prev) => [
      ...prev,
      { id: newId(), role: "user", content: prompt, ts: Date.now() },
    ]);
    assistantTurnId = newId();
    persistingSetTurns((prev) => [
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

    // Cycle D Sessions polish — register a chat_session row first
    // when this stream is opted in.  The sidebar's ``invalidateQueries``
    // call surfaces the new session BEFORE the pipeline emits its
    // first event, so the user sees their work in the list immediately.
    if (cfg.chatSessionMode) {
      try {
        const created = await sessionsApi.create({
          mode: cfg.chatSessionMode,
          title: prompt.slice(0, 60),
        });
        chatSessionId = (created as { id?: string }).id ?? null;
        invalidateSessionsList();
      } catch (err: unknown) {
        // Non-fatal — pipeline can still run; sidebar just won't
        // show this session until a future load.
        console.warn("[chat-stream] chat_session create failed:", err);
      }
    }

    try {
      const body = cfg.buildStartBody(prompt) as Record<string, unknown>;
      // Forward the chat_session_id so the backend can persist the
      // pipeline → chat_session linkage (already supported by
      // /api/code/start; opportunistically harmless on
      // /api/local-ai/research/start + /api/thinking/think which
      // ignore unknown fields).
      if (chatSessionId && body && typeof body === "object" && !("chat_session_id" in body)) {
        body.chat_session_id = chatSessionId;
      }
      const resp = await api.post<StartResp>(cfg.startPath, body);
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
      bumpChatSession();
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
    bumpChatSession();
  };

  /** Cycle D Sessions polish — bump the linked chat_session's
   *  ``updated_at`` so the sidebar moves the row to the "Now" group
   *  and the derived activity dot pulses on the most recent run.
   *  Fire-and-forget; failure is logged and swallowed. */
  const bumpChatSession = (): void => {
    if (!chatSessionId) return;
    void sessionsApi
      .update(chatSessionId, {})
      .then(() => invalidateSessionsList())
      .catch((err) => {
        console.warn("[chat-stream] sessions.update on finish failed:", err);
      });
  };

  const pushTurn = (turn: Omit<ChatTurn, "id">): void => {
    persistingSetTurns((prev) => [...prev, { ...turn, id: newId() }]);
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

  // Cycle F Sprint 5 — approval-required events from the SSE
  // bridge (document_processor/api/approval/bridge.py).  Push the
  // payload as a fresh ChatTurn so the MessageThread switches to
  // ApprovalPrompt.tsx for this turn.  The card manages its own
  // resolution state via POST /api/approval/{request_id}; we
  // don't subscribe to `approval_resolved` here (the POST result
  // updates the card directly).
  if (type === "approval_required") {
    const requestId =
      typeof ev.request_id === "string" ? ev.request_id : "";
    const toolName =
      typeof ev.tool_name === "string" ? ev.tool_name : "unknown";
    if (!requestId) return null;
    const args =
      ev.arguments && typeof ev.arguments === "object"
        ? (ev.arguments as Record<string, unknown>)
        : {};
    const category =
      typeof ev.category === "string" ? ev.category : "unclassified";
    const actorRole =
      typeof ev.actor_role === "string" ? ev.actor_role : null;
    const timeoutS =
      typeof ev.timeout_s === "number" ? ev.timeout_s : 90;
    return {
      pushTurn: {
        role: "approval",
        // content is rendered by ApprovalPrompt; leave a short
        // human-readable fallback for non-rendering consumers.
        content: `Approval required: ${toolName}`,
        ts: Date.now(),
        approval: {
          request_id: requestId,
          tool_name: toolName,
          category,
          arguments: args,
          actor_role: actorRole,
          timeout_s: timeoutS,
          status: "pending",
        },
      },
    };
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


/**
 * Research-specific reducer.  The Research backend emits a rich
 * progress stream — phase markers, sub-questions, sources being
 * added, "analyzing 3/8" status — and a final ``report_ready``
 * carrying the markdown report.  ``SIMPLE_TEXT_REDUCER`` only
 * understood ``research_complete`` (which the backend NEVER emits),
 * so research output rendered as the literal "(done)".
 *
 * Behaviour:
 *
 * * Progress events build up a ``_— Phase: …_`` / ``_Source: …_``
 *   trail in italics so the user sees the research happening live.
 * * ``analyzing_source`` events REPLACE the last status line so the
 *   "Analyzing 3/8: …" counter ticks in place rather than appending.
 * * ``report_ready`` REPLACES the entire buffer with the final
 *   markdown — the trail was preamble, the report is the answer.
 * * ``done`` finalises whatever is in the buffer.
 *
 * Wire it via ``getChatStream({reduce: RESEARCH_REDUCER, …})`` —
 * routes/Research.tsx is the canonical consumer.
 */
export const RESEARCH_REDUCER: EventReducer = (ev) => {
  const type = String(ev.type ?? "");

    // ── snapshot: stream-open replay.  Treat sources/phases the
    //    same way live events do, BUT we only get a single shot at
    //    the snapshot so we accumulate everything into one append.
    if (type === "snapshot" || type === "research_snapshot") {
      const events = Array.isArray(ev.events) ? ev.events : null;
      if (!events) return null;
      let trail = "";
      for (const e of events as Array<Record<string, unknown>>) {
        const t = String(e.type ?? "");
        if (t === "phase_start") {
          const phase = String(e.phase ?? "");
          if (phase) trail += `_— Phase: ${phase}_\n`;
        } else if (t === "sub_question") {
          const idx = Number(e.index ?? 0);
          const q = String(e.question ?? "");
          if (q) trail += `_Sub-question ${idx + 1}: ${q}_\n`;
        } else if (t === "source_added") {
          const title = String(e.title ?? e.url ?? "(untitled)");
          const url = String(e.url ?? "");
          trail += url
            ? `_Source: [${title}](${url})_\n`
            : `_Source: ${title}_\n`;
        } else if (t === "report_ready") {
          // If the snapshot already contains the final report, skip
          // the trail — we'll emit ``replace`` on the report.
          const md = typeof e.markdown === "string" ? e.markdown : "";
          if (md) {
            return { replace: md, tag: "report" };
          }
        }
      }
      return trail ? { append: trail, tag: "research" } : null;
    }

    // ── phase markers
    if (type === "phase_start" || type.endsWith("_phase_start")) {
      const phase = String(ev.phase ?? ev.label ?? "");
      return phase
        ? { append: `_— Phase: ${phase}_\n`, tag: `phase: ${phase}` }
        : null;
    }
    if (type === "phase_complete" || type.endsWith("_phase_complete")) {
      // Swallow — the next phase_start (or report_ready) is the
      // visible signal.
      return null;
    }

    // ── sub-questions
    if (type === "sub_question") {
      const idx = Number(ev.index ?? 0);
      const q = String(ev.question ?? "");
      return q
        ? { append: `_Sub-question ${idx + 1}: ${q}_\n`, tag: "research" }
        : null;
    }

    // ── search lifecycle (chatty; swallow)
    if (
      type === "search_start" ||
      type === "search_done" ||
      type === "scrape_start"
    ) {
      return null;
    }

    // ── source added
    if (type === "source_added") {
      const title = String(ev.title ?? ev.url ?? "(untitled)");
      const url = String(ev.url ?? "");
      const line = url
        ? `_Source: [${title}](${url})_\n`
        : `_Source: ${title}_\n`;
      return { append: line, tag: "research" };
    }

    // ── analyzing N/M source — replace the LAST analyzing line so
    //    the counter ticks in place.
    if (type === "analyzing_source") {
      const idx = Number(ev.index ?? 0);
      const total = Number(ev.total ?? 0);
      const title = String(ev.title ?? "");
      // Append a fresh "Analyzing N/M: title" line per event.  The
      // reducer is pure on ``ev`` only — we can't read the buffer
      // to do an in-place tick, so each source's analysis becomes
      // its own line.  Acceptable: the trail is meant to be
      // user-scannable, not a tight progress bar.
      return {
        append: `_Analyzing ${idx + 1}/${total}: ${title}_\n`,
        tag: "research",
      };
    }

    // ── relevance filtering
    if (type === "relevance_filter") {
      const filtered = Number(ev.filtered_out ?? 0);
      const kept = Number(ev.kept ?? 0);
      return filtered > 0
        ? {
            append: `_Filtered ${filtered} sources for relevance (kept ${kept})._\n`,
            tag: "research",
          }
        : null;
    }

    // ── final report — replace entire buffer with the markdown.
    if (type === "report_ready" || type === "research_complete") {
      const md = typeof ev.markdown === "string"
        ? ev.markdown
        : typeof ev.report === "string"
          ? ev.report
          : typeof ev.content === "string"
            ? ev.content
            : "";
      return md
        ? { replace: md, tag: "report" }
        : null;
    }

    // ── stream terminators
    if (type === "done") {
      return { done: true, tag: "done" };
    }
    if (type === "error" || type.endsWith("_error")) {
      const msg = String(ev.message ?? ev.error ?? "research error");
      return { error: msg };
    }
    if (type === "cancelled" || type.endsWith("_cancelled")) {
      return { tag: "cancelled", done: true };
    }

  // ── unknown — quietly drop (don't pollute the markdown trail
  //    with raw event JSON).
  return null;
};
