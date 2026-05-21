import { type Component, createMemo, Show } from "solid-js";
import { TopBar } from "../components/shell/TopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import { ChatComposer } from "../components/chat/ChatComposer";
import { MessageThread } from "../components/chat/MessageThread";
import { Button, StatusPill, type Status } from "../components/ui";
import { t } from "../i18n";
import {
  getChatStream,
  SIMPLE_TEXT_REDUCER,
  type EventReducer,
} from "../lib/chat-stream";

interface ConsortiumRequest {
  goal: string;
  depth: string;
  language: string;
  allow_external_research: boolean;
}

/**
 * Consortium mode Ã¢â‚¬â€ meta-orchestrator running Research, Thinking,
 * and Build sequentially against a single goal.  Wall-clock can run
 * 8Ã¢â‚¬â€œ15 min for deep depth, so the ConnectionBanner + Cancel
 * affordance matter.
 */
export const Consortium: Component = () => {
  // Override the reducer to strip the ``consortium_`` prefix from
  // phase tags so the UI shows "phase: scope" instead of
  // "phase: consortium_phase_start".
  const reducer: EventReducer = (ev) => {
    const patch = SIMPLE_TEXT_REDUCER(ev);
    if (!patch) return null;
    if (patch.tag) {
      patch.tag = patch.tag.replace(/^phase:\s*consortium_/, "phase: ");
    }
    return patch;
  };

  const stream = getChatStream<ConsortiumRequest>({
    startPath: "/api/consortium/start",
    buildStartBody: (goal) => ({
      goal,
      depth: "medium",
      language: "en",
      allow_external_research: true,
    }),
    eventsPath: (sid) => `/api/consortium/${sid}/events`,
    cancelPath: (sid) => `/api/consortium/${sid}/cancel`,
    reduce: reducer,
    // Cycle D Sessions polish Ã¢â‚¬â€ register the consortium run in the
    // sidebar so the user sees their goal listed immediately.
    chatSessionMode: "consortium",
  });

  const headerStatus = createMemo<Status>(() => {
    const s = stream.status();
    if (s === "open") return stream.busy() ? "warming" : "healthy";
    if (s === "connecting" || s === "reconnecting") return "warming";
    if (s === "offline") return "failed";
    return stream.busy() ? "warming" : "healthy";
  });

  return (
    <div data-mode="consortium" class="flex h-full flex-col">
      <TopBar
        title={t("consortium.title")}
        subtitle={t("consortium.subtitle")}
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
              {t("consortium.empty.title")}
            </p>
            <p class="mt-2 text-sm text-text-subtle">
              {t("consortium.empty.body")}
            </p>
          </div>
        }
      />
      <ChatComposer
        onSubmit={stream.start}
        busy={stream.busy()}
        onCancel={stream.cancel}
        placeholder={t("consortium.composer.placeholder")}
      />
    </div>
  );
};
