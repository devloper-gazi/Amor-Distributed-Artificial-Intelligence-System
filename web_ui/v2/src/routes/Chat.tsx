/**
 * Cycle C Sprint 4 Day 1+4 Ã¢â‚¬â€ unified chat surface (preview).
 *
 * Single-page chat that hosts UnifiedComposer + a readback panel
 * showing the parsed mode, attachment count, and a Day-4 ToolCallCard
 * preview synthesized from a canned ``execution_*`` sequence so the
 * Sprint 4 visuals are reachable without touching Build/Research's
 * live pipelines.
 *
 * Existing per-mode routes (/build, /research, /thinking, ...)
 * stay live as the canonical workspaces; /chat is the new
 * mode-agnostic entry that the Sprint 4 plan promises.  Live pipeline
 * dispatch (chat-stream singleton wire-up) lands in a follow-up.
 */

import { type Component, For, Show, createSignal } from "solid-js";

import { TopBar } from "../components/shell/TopBar";
import { BottomSheet } from "../components/shell/BottomSheet";
import {
  UnifiedComposer,
  type ComposerSubmission,
} from "../components/chat/UnifiedComposer";
import { ToolCallCard } from "../components/chat/ToolCallCard";
import { Badge } from "../components/ui";
import { type ModeKey } from "../lib/types";
import { modeLabel, t } from "../i18n";
import {
  ingestToolEvent,
  toToolEvents,
  type ToolCallFrame,
} from "../lib/tool-stream";

interface SubmittedTurn {
  text: string;
  mode: ModeKey;
  ts: number;
  attachments: number;
  /** Synthetic tool-call frames so Day-4 visuals render without a
   *  live pipeline dispatch.  Replaced by real frames once chat-stream
   *  wiring lands. */
  toolFrames: ToolCallFrame[];
}

/**
 * Synthesize a minimal canned tool-call trace for the resolved mode
 * so the user sees ToolCallCards animate end-to-end.  Pure local Ã¢â‚¬â€
 * no network.
 */
function synthFrames(mode: ModeKey): ToolCallFrame[] {
  const events = [
    { type: "execution_start", iteration: 0, language: mode === "build" ? "python" : "text" },
    { type: "execution_install_packages", iteration: 0, packages: [] },
    {
      type: "execution_result",
      iteration: 0,
      language: mode === "build" ? "python" : "text",
      exit_code: 0,
      stdout: `(synthetic preview Ã¢â‚¬â€ Day 4 tool-card rendering for ${mode} mode)`,
      stderr: "",
      duration_ms: 12,
    },
    {
      type: "review_ready",
      iteration: 0,
      score: 88,
      summary: "Stubbed Ã¢â‚¬â€ live review awaits chat-stream wiring.",
      findings: [],
    },
  ];

  let frames = new Map<string, ToolCallFrame>();
  for (const ev of events) {
    for (const tev of toToolEvents(ev)) {
      frames = ingestToolEvent(frames, tev);
    }
  }
  return Array.from(frames.values());
}

export const Chat: Component = () => {
  const [history, setHistory] = createSignal<SubmittedTurn[]>([]);
  const [busy, setBusy] = createSignal(false);

  const handleSubmit = (text: string, mode: ModeKey) => {
    setHistory((h) => [
      ...h,
      {
        text,
        mode,
        ts: Date.now(),
        attachments: 0,
        toolFrames: synthFrames(mode),
      },
    ]);
    setBusy(true);
    setTimeout(() => setBusy(false), 800);
  };

  const handleSubmitRich = (sub: ComposerSubmission) => {
    setHistory((h) => {
      const last = h[h.length - 1];
      if (!last) return h;
      const updated = { ...last, attachments: sub.attachments.length };
      return [...h.slice(0, -1), updated];
    });
  };

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title={t("chat.title")}
        subtitle={t("chat.subtitle")}
      />

      <div class="flex-1 overflow-auto px-6 py-6">
        <Show
          when={history().length > 0}
          fallback={
            <div class="mx-auto max-w-md text-center text-text-subtle">
              <p class="text-base text-text-display">{t("chat.preview.title")}</p>
              <p class="mt-2 text-sm whitespace-pre-line">
                {t("chat.preview.body", {
                  cmds: "/build, /research, /think, /consortium, /sentinel, /system",
                })}
              </p>
              <p class="mt-3 text-[0.7rem] text-text-subtle">
                {t("chat.preview.hint")}
              </p>
            </div>
          }
        >
          <ul class="space-y-3">
            <For each={history()}>
              {(turn) => (
                <li class="space-y-2">
                  <div class="rounded-md border border-border-subtle bg-bg-elevated p-3">
                    <div class="mb-1 flex items-center gap-2 text-[0.7rem] text-text-subtle">
                      <Badge>{modeLabel(turn.mode)}</Badge>
                      <span class="tabular-nums">
                        {new Date(turn.ts).toLocaleTimeString()}
                      </span>
                      <Show when={turn.attachments > 0}>
                        <span>Ã‚Â· {turn.attachments} attachment{turn.attachments === 1 ? "" : "s"}</span>
                      </Show>
                    </div>
                    <pre class="whitespace-pre-wrap font-mono text-xs text-text-display">
                      {turn.text}
                    </pre>
                  </div>
                  <Show when={turn.toolFrames.length > 0}>
                    <div class="space-y-2 pl-4 border-l border-border-subtle">
                      <For each={turn.toolFrames}>
                        {(frame) => <ToolCallCard frame={frame} />}
                      </For>
                    </div>
                  </Show>
                </li>
              )}
            </For>
          </ul>
        </Show>
      </div>

      <BottomSheet>
        <UnifiedComposer
          onSubmit={handleSubmit}
          onSubmitRich={handleSubmitRich}
          busy={busy()}
        />
      </BottomSheet>
    </div>
  );
};
