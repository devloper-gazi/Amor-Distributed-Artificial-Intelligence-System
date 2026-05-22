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
// Cycle UI v2.5 Phase 3 — seed-prompt grid shown when the thread
// is empty.  Clicking a card pre-fills the composer + sets the
// suggested mode without auto-submitting.
// Cycle UI v2.6 (Karar A + H) — atmospheric halo backdrop, mode-tinted.
import { Halo } from "../components/chat/Halo";
// Cycle UI v2.6.1 — Greeting moves OUT of EmptyState into UnifiedChat
// so it can sit side-by-side with a centered composer on empty thread.
import { Greeting } from "../components/chat/Greeting";
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

  // Cycle UI v2.6 (Karar F) — placeholder rotation.  Cycles every 8s
  // through 4 i18n variants so the empty composer feels alive.  When
  // the classifier reports `low_confidence`, lock to variant 0 (the
  // neutral "Ne istersen sor — doğru modu ben seçerim" line) so the
  // surface doesn't keep moving while the user is mid-thought.
  // `prefers-reduced-motion` short-circuits to variant 0 too.
  const [placeholderIdx, setPlaceholderIdx] = createSignal(0);
  const placeholderText = createMemo<string>(() => {
    const r = classifier.result();
    const reduced =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduced || (r && r.low_confidence)) return t("composer.placeholder.0");
    return t(`composer.placeholder.${placeholderIdx()}`);
  });
  onMount(() => {
    if (typeof window === "undefined") return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const id = window.setInterval(() => {
      setPlaceholderIdx((i) => (i + 1) % 4);
    }, 8000);
    onCleanup(() => window.clearInterval(id));
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

  // Cycle UI v2.6 (Karar H) — composer focus → Halo focused signal.
  // Document-level focusin/focusout bubbles up through Solid's tree;
  // we detect whether the focus target is inside the composer (data-
  // attribute scoped) and flip a signal Halo reads.  Pure DOM event,
  // no prop drill into UnifiedComposer's 861-LOC internals.
  const [composerFocused, setComposerFocused] = createSignal(false);
  onMount(() => {
    if (typeof document === "undefined") return;
    const isComposer = (el: EventTarget | null): boolean =>
      el instanceof HTMLElement &&
      !!el.closest('[data-amor-composer="unified"]');
    const onIn  = (ev: FocusEvent) => { if (isComposer(ev.target)) setComposerFocused(true);  };
    const onOut = (ev: FocusEvent) => { if (isComposer(ev.target)) setComposerFocused(false); };
    document.addEventListener("focusin",  onIn);
    document.addEventListener("focusout", onOut);
    onCleanup(() => {
      document.removeEventListener("focusin",  onIn);
      document.removeEventListener("focusout", onOut);
    });
  });

  // Cycle UI v2.6 (Karar M) — global keyboard shortcuts.  Native
  // ``window.addEventListener`` instead of a dep (`@solid-primitives/
  // keyboard`) per Q1 user decision.  Platform-aware: macOS uses
  // Cmd (metaKey), Win/Linux uses Ctrl (ctrlKey).
  //
  // Bindings:
  //   * Cmd/Ctrl+N  — new chat (clear current stream + textarea)
  //   * Cmd/Ctrl+/  — focus the composer + insert "/" so the slash
  //                   overlay opens (UnifiedComposer already wires
  //                   the slash-prefix detection)
  //   * Cmd/Ctrl+B  — sidebar toggle (CustomEvent listened by Sidebar)
  //   * Esc         — already handled by Kobalte overlay close; we
  //                   only catch it here when a download/preview etc.
  //                   bubbles up un-handled.  No-op by default.
  //
  // Cmd+K is intentionally NOT bound here — CommandPalette owns it
  // (web_ui/v2/src/components/shell/CommandPalette.tsx) and reacts
  // to its own keydown listener.  Adding a second handler would
  // double-toggle.
  onMount(() => {
    if (typeof window === "undefined") return;
    const isMac = /Mac|iPhone|iPod|iPad/i.test(navigator.platform || "");
    const handler = (ev: KeyboardEvent) => {
      const mod = isMac ? ev.metaKey : ev.ctrlKey;
      if (!mod) return;
      const key = ev.key.toLowerCase();
      // Cmd+N — new chat
      if (key === "n" && !ev.shiftKey && !ev.altKey) {
        ev.preventDefault();
        const api = streamApi();
        if (api) void api.cancel();
        // Route to clean / so deep-link state (?c=…) drops too.
        if (window.location.pathname !== "/" || window.location.search) {
          window.history.replaceState(null, "", "/");
        }
        // Wipe in-place by clearing the stream signal — composer
        // textarea is left untouched (user might still want to
        // re-send).  If they want a fully fresh state they'll
        // navigate or refresh.
        setStreamApi(undefined);
        return;
      }
      // Cmd+/ — focus composer + open slash overlay
      if (key === "/" && !ev.shiftKey && !ev.altKey) {
        ev.preventDefault();
        window.dispatchEvent(
          new CustomEvent("amor:focus-composer", { detail: { prefix: "/" } }),
        );
        return;
      }
      // Cmd+B — sidebar toggle
      if (key === "b" && !ev.shiftKey && !ev.altKey) {
        ev.preventDefault();
        window.dispatchEvent(new CustomEvent("amor:sidebar-toggle"));
        return;
      }
    };
    window.addEventListener("keydown", handler);
    onCleanup(() => window.removeEventListener("keydown", handler));
  });

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
    <div
      class="flex h-dvh flex-col overflow-hidden"
      data-amor-route="unified-chat"
      style={{
        // Cycle UI Phase 4.3 — keep the layout glued to the visible
        // viewport on iOS Safari when the on-screen keyboard pushes
        // the address bar around.  `100dvh` already adapts; the
        // safe-area padding handles the home-indicator gap below.
        "padding-bottom": "env(safe-area-inset-bottom, 0)",
      }}
    >
      {/* Cycle UI v2.6 — atmospheric halo backdrop (Karar A).  Sits
          behind everything via position:fixed + z-index:-1; mode prop
          drives the tint, focus state handled in D10 (Karar H). */}
      <Halo mode={activeMode() ?? suggestedMode()} focused={composerFocused()} />
      {/* Cycle UI v2.6.1 — minimal TopBar: title-only, no subtitle.
          The mode breadcrumb is intentionally hidden now — modes are
          implicit via auto-classifier; surfacing them in the topbar
          turned the chat surface into a control panel.  TopBar
          collapses to a thin chrome strip; sessions live in sidebar. */}
      <TopBar title={t("chat.unified_title")} />
      <ConnectionBanner status={status()} />
      {/* Cycle UI v2.6.1 (Karar F-part2) — chat-only layout.
          When the thread is empty, composer + greeting share the
          centered hero area.  After first turn, composer drops to
          its conventional bottom-fixed slot.  Pure CSS layout
          switch — no UnifiedComposer internals touched. */}
      <Show
        when={turns().length > 0}
        fallback={
          <main class="flex flex-1 flex-col items-center justify-center overflow-y-auto px-4">
            {/* Cycle UI v2.6.3 — wrap max-w-2xl → max-w-3xl, Gemini
                geniş composer pill-flow paterni.  Greeting margin
                composer'a 8 → 5 (daha sıkı, "alan" değil "konuşma"). */}
            <div class="amor-enter w-full max-w-3xl">
              <div class="mb-5 flex flex-col items-center">
                <Greeting />
              </div>
              {/* Composer mounted INSIDE the centered column when
                  thread is empty.  Same component instance — Solid
                  preserves state across the Show fallback switch
                  because we keep it mounted under a single key
                  via the Show's reactive boundary.  No state loss. */}
              <UnifiedComposer
                onSubmit={onSubmit}
                busy={busy()}
                onCancel={onCancel}
                initialMode={activeMode()}
                modeOverride={suggestedMode()}
                modeBadge={badgeText()}
                onTextChange={(text) => classifier.setPrompt(text)}
                placeholder={placeholderText()}
              />
            </div>
          </main>
        }
      >
        <main class="flex-1 overflow-y-auto">
          <MessageThread
            turns={turns()}
            emptyState={null}
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
          placeholder={placeholderText()}
        />
      </Show>
      <Show when={classifier.error()}>
        <div class="bg-bg-elevated-v25 px-5 py-1 text-[0.7rem] text-text-subtle">
          {t("classifier.error")}: {classifier.error()?.message}
        </div>
      </Show>
    </div>
  );
};

export default UnifiedChat;
