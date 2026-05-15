import { type Component, createMemo, createSignal, Show } from "solid-js";
import { TopBar } from "../components/shell/TopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import { ChatComposer } from "../components/chat/ChatComposer";
import { MessageThread } from "../components/chat/MessageThread";
import { Button, StatusPill, type Status } from "../components/ui";
import { t } from "../i18n";
import {
  getChatStream,
  SIMPLE_TEXT_REDUCER,
} from "../lib/chat-stream";

interface ThinkRequest {
  prompt: string;
  effort: string;
  /** Map of ClarifyingQuestion id → user's answer.  v2 skips the
   *  ``/api/thinking/analyze`` Q&A step for now, so the dict is
   *  always empty — the engine treats every clarification as
   *  "no answer given" and proceeds. */
  clarifications: Record<string, string>;
}

// Cycle D — per-mode effort persistence.  Thinking accepts the same
// 5-tier canonical scale as Build/Research (basic/medium/deep/expert/
// ultra) — see document_processor/thinking/models.py:80.
const STORAGE_KEY_THINKING_EFFORT = "amor.thinking.effort";
const THINKING_EFFORT_TIERS = [
  { value: "basic",  label_key: "effort.basic.label",  description_key: "effort.basic.description" },
  { value: "medium", label_key: "effort.medium.label", description_key: "effort.medium.description" },
  { value: "deep",   label_key: "effort.deep.label",   description_key: "effort.deep.description" },
  { value: "expert", label_key: "effort.expert.label", description_key: "effort.expert.description" },
  { value: "ultra",  label_key: "effort.ultra.label",  description_key: "effort.ultra.description" },
] as const;
type ThinkingEffort = (typeof THINKING_EFFORT_TIERS)[number]["value"];

function loadThinkingEffort(): ThinkingEffort {
  try {
    const raw = localStorage.getItem(STORAGE_KEY_THINKING_EFFORT);
    if (raw && THINKING_EFFORT_TIERS.some((t) => t.value === raw)) {
      return raw as ThinkingEffort;
    }
  } catch {
    // ignore
  }
  return "medium";
}

function saveThinkingEffort(value: ThinkingEffort): void {
  try {
    localStorage.setItem(STORAGE_KEY_THINKING_EFFORT, value);
  } catch {
    // ignore
  }
}

/**
 * Thinking mode — multi-step reasoning with streaming output.
 *
 * The backend has an ``/api/thinking/analyze`` endpoint that asks
 * clarifying questions before the run; v2 sends an empty
 * ``clarifications`` map for now (the engine treats missing answers
 * as "no answer given" and proceeds).
 */
export const Thinking: Component = () => {
  const [effort, setEffortRaw] = createSignal<ThinkingEffort>(loadThinkingEffort());
  const setEffort = (next: ThinkingEffort): void => {
    setEffortRaw(next);
    saveThinkingEffort(next);
  };

  const stream = getChatStream<ThinkRequest>({
    startPath: "/api/thinking/think",
    buildStartBody: (prompt) => ({
      prompt,
      effort: effort(),
      clarifications: {},
    }),
    eventsPath: (sid) => `/api/thinking/${sid}/events`,
    cancelPath: (sid) => `/api/thinking/${sid}/cancel`,
    reduce: SIMPLE_TEXT_REDUCER,
    // Cycle D Sessions polish — see Research.tsx for the rationale.
    chatSessionMode: "thinking",
  });

  const headerStatus = createMemo<Status>(() => {
    const s = stream.status();
    if (s === "open") return stream.busy() ? "warming" : "healthy";
    if (s === "connecting" || s === "reconnecting") return "warming";
    if (s === "offline") return "failed";
    return stream.busy() ? "warming" : "healthy";
  });

  return (
    <div data-mode="thinking" class="flex h-full flex-col">
      <TopBar
        title={t("thinking.title")}
        subtitle={t("thinking.subtitle")}
        actions={
          <Show when={stream.busy()}>
            <StatusPill status={headerStatus()} size="sm" />
            <Button variant="secondary" size="sm" onClick={stream.cancel}>
              {t("common.cancel")}
            </Button>
          </Show>
        }
      />
      <ConnectionBanner status={stream.status()} />
      <MessageThread
        turns={stream.turns()}
        emptyState={
          <div class="max-w-md text-center">
            <p class="text-base text-text-primary">
              {t("thinking.empty.title")}
            </p>
            <p class="mt-2 text-sm text-text-tertiary">
              {t("thinking.empty.body")}
            </p>
          </div>
        }
      />
      <ChatComposer
        onSubmit={stream.start}
        busy={stream.busy()}
        onCancel={stream.cancel}
        placeholder={t("thinking.composer.placeholder")}
        effortTiers={THINKING_EFFORT_TIERS}
        effortValue={effort()}
        onEffortChange={(v) => setEffort(v as ThinkingEffort)}
      />
    </div>
  );
};
