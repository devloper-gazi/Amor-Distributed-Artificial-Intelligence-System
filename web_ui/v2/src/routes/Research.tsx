import { type Component, createMemo, Show } from "solid-js";
import { TopBar } from "../components/shell/TopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import { ChatComposer } from "../components/chat/ChatComposer";
import { MessageThread } from "../components/chat/MessageThread";
import { Button, StatusPill, type Status } from "../components/ui";
import {
  getChatStream,
  SIMPLE_TEXT_REDUCER,
} from "../lib/chat-stream";

interface ResearchRequest {
  topic: string;
  depth: string;
}

/**
 * Research mode — single-shot autonomous research session.  The
 * pipeline is a single long-running task; SSE events report
 * progress, intermediate findings, and the final synthesis.
 *
 * Uses the shared singleton stream registry so navigation away from
 * /research and back doesn't wipe the conversation or kill an
 * in-flight pipeline.
 */
export const Research: Component = () => {
  const stream = getChatStream<ResearchRequest>({
    startPath: "/api/local-ai/research",
    buildStartBody: (topic) => ({ topic, depth: "medium" }),
    eventsPath: (sid) => `/api/local-ai/research/${sid}/events`,
    cancelPath: (sid) => `/api/local-ai/research/${sid}/cancel`,
    reduce: SIMPLE_TEXT_REDUCER,
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
        title="Research"
        subtitle="gather, summarise, cite"
        actions={
          <Show when={stream.busy()}>
            <StatusPill status={headerStatus()} size="sm" />
            <Button variant="secondary" size="sm" onClick={stream.cancel}>
              Cancel
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
              Ask a research question
            </p>
            <p class="mt-2 text-sm text-text-tertiary">
              AMOR will gather sources, synthesise a report, and cite
              every claim.  Streaming responses appear here as the
              backend produces them.
            </p>
          </div>
        }
      />
      <ChatComposer
        onSubmit={stream.start}
        busy={stream.busy()}
        onCancel={stream.cancel}
        placeholder="Research topic… (e.g. 'compare CRDT vs OT for collaborative editing')"
      />
    </div>
  );
};
