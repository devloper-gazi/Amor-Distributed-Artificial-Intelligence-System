/**
 * Cycle UI 2026-05-20 — Unified chat single-page route.
 *
 * Mounts at ``/`` as the new primary chat surface, replacing the
 * 6-route mode-segregated SPA.  Behaviour:
 *
 *   * Single composer with auto-mode preview pill driven by the
 *     `/api/chat/classify` debounced classifier (150 ms).
 *   * Manual mode override (ModePicker, slash commands) wins over
 *     classifier suggestion until the user submits.
 *   * On submit, dispatches to the classified mode's existing
 *     ``/api/{mode}/start`` endpoint with the appropriate body shape;
 *     the legacy mode routes stay running untouched.
 *   * Streaming uses the SAME ``openEventStream`` / reducer pipeline
 *     as the per-mode routes, so Approval cards, dedup, reconnect
 *     and SSE rotation all work out of the box.
 *
 * Phase 2 MVP — covers build / research / thinking / consortium /
 * sentinel / quickcode modes.  Each mode's submission body is shaped
 * by ``MODE_SUBMIT_ADAPTERS`` so the dispatch is purely data-driven.
 *
 * Phase 4 will add branching UI (BranchNavigator) on top of this.
 */

import {
  type Component,
  Show,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";
import { TopBar } from "../components/shell/TopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import { MessageThread } from "../components/chat/MessageThread";
import { UnifiedComposer } from "../components/chat/UnifiedComposer";
import {
  UNIFIED_REDUCER,
  createChatStream,
  type ChatStreamApi,
} from "../lib/chat-stream";
import {
  createDebouncedClassifier,
  classifyPrompt,
} from "../lib/intent-classifier";
import type { ModeKey } from "../lib/types";
import { t } from "../i18n";

// ─── Per-mode dispatch adapters ─────────────────────────────────────────

interface ModeAdapter {
  startPath: string;
  eventsPath: (sid: string) => string;
  cancelPath: (sid: string) => string | null;
  buildStartBody: (prompt: string) => Record<string, unknown>;
  /** Backend chat_session ``mode`` value the sidebar groups by. */
  chatSessionMode: string;
}

// Cycle UI Phase 3 — Mode → endpoint map.  Reducer field dropped; the
// single UNIFIED_REDUCER (imported above) dispatches via event_registry
// so every mode shares the same dispatch core + the same approval /
// snapshot / terminator handling.
const MODE_ADAPTERS: Record<ModeKey, ModeAdapter> = {
  build: {
    startPath: "/api/code/start",
    eventsPath: (sid) => `/api/code/${sid}/events`,
    cancelPath: (sid) => `/api/code/${sid}/cancel`,
    buildStartBody: (prompt) => ({ prompt, effort: "medium" }),
    chatSessionMode: "code",
  },
  research: {
    startPath: "/api/local-ai/research",
    eventsPath: (sid) => `/api/local-ai/research/${sid}/events`,
    cancelPath: (sid) => `/api/local-ai/research/${sid}/cancel`,
    buildStartBody: (prompt) => ({ topic: prompt, depth: "medium" }),
    chatSessionMode: "research",
  },
  thinking: {
    startPath: "/api/thinking/think",
    eventsPath: (sid) => `/api/thinking/${sid}/events`,
    cancelPath: (sid) => `/api/thinking/${sid}/cancel`,
    buildStartBody: (prompt) => ({ prompt, effort: "medium" }),
    chatSessionMode: "thinking",
  },
  consortium: {
    startPath: "/api/consortium/start",
    eventsPath: (sid) => `/api/consortium/${sid}/events`,
    cancelPath: (sid) => `/api/consortium/${sid}/cancel`,
    buildStartBody: (prompt) => ({ prompt, depth: "medium" }),
    chatSessionMode: "consortium",
  },
  sentinel: {
    startPath: "/api/sentinel/start",
    eventsPath: (sid) => `/api/sentinel/${sid}/events`,
    cancelPath: (sid) => `/api/sentinel/${sid}/cancel`,
    buildStartBody: (prompt) => ({ prompt, scan_profile: "standard" }),
    chatSessionMode: "sentinel",
  },
  quickcode: {
    startPath: "/api/quick-code/start",
    eventsPath: (sid) => `/api/quick-code/${sid}/events`,
    cancelPath: (sid) => `/api/quick-code/${sid}/cancel`,
    buildStartBody: (prompt) => ({ prompt, mode: "quick" }),
    chatSessionMode: "code",  // backed by code_intelligence storage
  },
  system: {
    // Legacy-only; the 6-class classifier never returns "system",
    // but slash command /system can.  Falls back to thinking endpoint
    // (no dedicated system backend exists per Decision 3).
    startPath: "/api/thinking/think",
    eventsPath: (sid) => `/api/thinking/${sid}/events`,
    cancelPath: (sid) => `/api/thinking/${sid}/cancel`,
    buildStartBody: (prompt) => ({ prompt, effort: "medium" }),
    chatSessionMode: "thinking",
  },
};

export const UnifiedChat: Component = () => {
  const [activeMode, setActiveMode] = createSignal<ModeKey>("build");
  const [streamApi, setStreamApi] = createSignal<ChatStreamApi | null>(null);

  // Classifier debouncer drives the auto-mode preview pill.  Result is
  // null until the user types enough text + the 150 ms debounce
  // elapses + the server returns.  Composer reads the result and
  // passes mode/badge as the ``modeOverride`` + ``modeBadge`` props.
  const classifier = createDebouncedClassifier();

  // The composer's modeOverride is the classifier's suggestion (when
  // confidence is high) OR null when low.  The composer maintains its
  // own "user picked" lock; once they click ModePicker, the override
  // is ignored.
  const suggestedMode = createMemo<ModeKey | undefined>(() => {
    const r = classifier.result();
    if (!r) return undefined;
    if (r.low_confidence) {
      // Still surface the guess via badge text but don't override the
      // composer's mode — let user explicit-pick or default ride.
      return undefined;
    }
    return r.mode as ModeKey;
  });

  const badgeText = createMemo<string | undefined>(() => {
    if (classifier.pending()) return t("classifier.thinking");
    const r = classifier.result();
    if (!r) return undefined;
    if (r.low_confidence) return t("classifier.uncertain");
    return t("classifier.auto");
  });

  // Lazily create the stream API for the chosen mode + prompt.  Each
  // submission spawns a new stream singleton keyed on startPath.
  // All 6 modes share UNIFIED_REDUCER — per-mode dispatch happens
  // inside the reducer via event_registry.
  const ensureStream = (mode: ModeKey): ChatStreamApi => {
    const adapter = MODE_ADAPTERS[mode];
    // createChatStream is cached at module level by startPath, so
    // repeated calls reuse the same turns/busy/status signals.
    const stream = createChatStream<Record<string, unknown>>({
      startPath: adapter.startPath,
      buildStartBody: adapter.buildStartBody,
      eventsPath: adapter.eventsPath,
      cancelPath: adapter.cancelPath,
      reduce: UNIFIED_REDUCER,
      chatSessionMode: adapter.chatSessionMode,
    });
    setStreamApi(stream);
    setActiveMode(mode);
    return stream;
  };

  const onSubmit = async (text: string, mode: ModeKey) => {
    const stream = ensureStream(mode);
    // Snapshot the classifier result BEFORE cancelling so we can
    // thread it into the turn for MessageBubble's hover tooltip.
    const cm = classifier.result();
    const classifierMeta = cm
      ? {
          top1: cm.alternatives[0]?.[0] ?? cm.mode,
          top1_score: cm.top1_score,
          top2: cm.alternatives[1]?.[0] ?? cm.alternatives[0]?.[0] ?? "",
          top2_score: cm.top2_score,
          confidence: cm.confidence,
          low_confidence: cm.low_confidence,
        }
      : undefined;
    classifier.cancel();
    await stream.start(text, { mode, classifierMeta });
  };

  const onCancel = async () => {
    const api = streamApi();
    if (api) await api.cancel();
  };

  const turns = createMemo(() => streamApi()?.turns() ?? []);
  const busy = createMemo(() => streamApi()?.busy() ?? false);
  const status = createMemo(() => streamApi()?.status() ?? "closed");

  onCleanup(() => classifier.cancel());

  // Hydrate from ?c=<session_id> deep-link.  Phase 2 placeholder —
  // Phase 4 will fetch /api/sessions/{id}/branch and replay turns.
  onMount(() => {
    const params = new URLSearchParams(window.location.search);
    const cid = params.get("c");
    if (cid) {
      // Phase 4 hook — for now just no-op so the URL is preserved.
      // Future: const branch = await fetch(`/api/sessions/${cid}/branch`)
      //        and seed the turns from the branch payload.
    }

    // Cycle UI Phase 3.4 — eager-prefetch the classifier on mount so
    // the user's first real prompt doesn't trigger the 3-5 s MiniLM
    // first-load.  Fire-and-forget: swallow any error (network /
    // auth / cold-start) — the actual classifier path retries on
    // every user keystroke, so a failed prefetch only loses the
    // warmup benefit, not functionality.
    void classifyPrompt("warmup").catch(() => undefined);
  });

  return (
    <div class="flex h-dvh flex-col" data-amor-route="unified-chat">
      <TopBar
        title={t("chat.unified_title")}
        subtitle={t("chat.unified_subtitle")}
      />
      <ConnectionBanner status={status()} />
      <main class="flex-1 overflow-y-auto">
        <MessageThread
          turns={turns()}
          emptyState={
            <div class="mx-auto max-w-2xl px-6 py-16 text-center">
              <h1 class="text-2xl font-semibold text-text-primary">
                {t("chat.unified_empty_title")}
              </h1>
              <p class="mt-3 text-sm text-text-secondary">
                {t("chat.unified_empty_subtitle")}
              </p>
              <p class="mt-2 text-xs text-text-tertiary">
                {t("chat.unified_empty_hint")}
              </p>
            </div>
          }
        />
      </main>
      <UnifiedComposer
        onSubmit={onSubmit}
        busy={busy()}
        onCancel={onCancel}
        initialMode={activeMode()}
        modeOverride={suggestedMode()}
        modeBadge={badgeText()}
        onTextChange={(text) => classifier.setPrompt(text)}
        placeholder={t("chat.unified_placeholder")}
      />
      <Show when={classifier.error()}>
        <div class="bg-bg-secondary px-5 py-1 text-[0.7rem] text-text-tertiary">
          {t("classifier.error")}: {classifier.error()?.message}
        </div>
      </Show>
    </div>
  );
};

export default UnifiedChat;
