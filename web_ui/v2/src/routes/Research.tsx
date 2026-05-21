import {
  type Component,
  createMemo,
  createSignal,
  onMount,
  Show,
} from "solid-js";
import { TopBar } from "../components/shell/TopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import {
  ChatComposer,
  type EffortTierOption,
} from "../components/chat/ChatComposer";
import { MessageThread } from "../components/chat/MessageThread";
import { Button, StatusPill, type Status } from "../components/ui";
import { t } from "../i18n";
import {
  getChatStream,
  RESEARCH_REDUCER,
} from "../lib/chat-stream";

interface ResearchRequest {
  topic: string;
  depth: string;
}

/** Cycle C polish Ã¢â‚¬â€ five canonical depth tiers the backend
 *  ``LocalAIResearchRequest.depth`` accepts (basic/medium/deep/
 *  expert/ultra).  Order is significant: it's how the segmented
 *  control renders left-to-right. */
const RESEARCH_EFFORT_TIERS: ReadonlyArray<EffortTierOption> = [
  { value: "basic",  label_key: "effort.basic.label",  description_key: "effort.basic.description" },
  { value: "medium", label_key: "effort.medium.label", description_key: "effort.medium.description" },
  { value: "deep",   label_key: "effort.deep.label",   description_key: "effort.deep.description" },
  { value: "expert", label_key: "effort.expert.label", description_key: "effort.expert.description" },
  { value: "ultra",  label_key: "effort.ultra.label",  description_key: "effort.ultra.description" },
];

const VALID_EFFORTS = new Set(RESEARCH_EFFORT_TIERS.map((t) => t.value));
const LS_EFFORT_KEY = "amor.research.effort";
const DEFAULT_EFFORT = "medium";

const loadEffort = (): string => {
  try {
    const raw = localStorage.getItem(LS_EFFORT_KEY);
    if (raw && VALID_EFFORTS.has(raw)) return raw;
  } catch {
    // ignore
  }
  return DEFAULT_EFFORT;
};

const saveEffort = (next: string): void => {
  try {
    localStorage.setItem(LS_EFFORT_KEY, next);
  } catch {
    // ignore
  }
};


/**
 * Research mode Ã¢â‚¬â€ single-shot autonomous research session.  The
 * pipeline is a single long-running task; SSE events report
 * progress, intermediate findings, and the final synthesis.
 *
 * Uses the shared singleton stream registry so navigation away from
 * /research and back doesn't wipe the conversation or kill an
 * in-flight pipeline.
 *
 * Cycle C polish (post-close-out) Ã¢â‚¬â€ uses ``RESEARCH_REDUCER`` so
 * the rich progress event stream (phase markers, sources, final
 * markdown report) actually renders.  Effort segmented control
 * exposes the backend's five depth tiers; persists the user's
 * choice in ``localStorage["amor.research.effort"]``.
 */
export const Research: Component = () => {
  const [effort, setEffortSignal] = createSignal<string>(DEFAULT_EFFORT);

  onMount(() => {
    setEffortSignal(loadEffort());
  });

  const setEffort = (next: string): void => {
    if (!VALID_EFFORTS.has(next)) return;
    setEffortSignal(next);
    saveEffort(next);
  };

  const stream = getChatStream<ResearchRequest>({
    startPath: "/api/local-ai/research",
    // ``buildStartBody`` is captured once at mount but called per
    // submit, so reading ``effort()`` here gives the current value.
    buildStartBody: (topic) => ({ topic, depth: effort() }),
    eventsPath: (sid) => `/api/local-ai/research/${sid}/events`,
    cancelPath: (sid) => `/api/local-ai/research/${sid}/cancel`,
    reduce: RESEARCH_REDUCER,
    // Cycle D Sessions polish Ã¢â‚¬â€ register a chat_session row in the
    // sidebar as soon as the user submits a research query, and bump
    // updated_at when the stream finishes / errors / is cancelled.
    chatSessionMode: "research",
  });

  const headerStatus = createMemo<Status>(() => {
    const s = stream.status();
    if (s === "open") return stream.busy() ? "warming" : "healthy";
    if (s === "connecting" || s === "reconnecting") return "warming";
    if (s === "offline") return "failed";
    return stream.busy() ? "warming" : "healthy";
  });

  return (
    <div data-mode="research" class="flex h-full flex-col">
      <TopBar
        title={t("research.title")}
        subtitle={t("research.subtitle")}
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
            <p class="text-base text-text-display">
              {t("research.empty.title")}
            </p>
            <p class="mt-2 text-sm text-text-subtle">
              {t("research.empty.body")}
            </p>
          </div>
        }
      />
      <ChatComposer
        onSubmit={stream.start}
        busy={stream.busy()}
        onCancel={stream.cancel}
        placeholder={t("research.composer.placeholder")}
        effortTiers={RESEARCH_EFFORT_TIERS}
        effortValue={effort()}
        onEffortChange={setEffort}
      />
    </div>
  );
};
