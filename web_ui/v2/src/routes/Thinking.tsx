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

interface ThinkRequest {
  prompt: string;
  effort: string;
  /** Map of ClarifyingQuestion id → user's answer.  v2 skips the
   *  ``/api/thinking/analyze`` Q&A step for now, so the dict is
   *  always empty — the engine treats every clarification as
   *  "no answer given" and proceeds. */
  clarifications: Record<string, string>;
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
  const stream = getChatStream<ThinkRequest>({
    startPath: "/api/thinking/think",
    buildStartBody: (prompt) => ({
      prompt,
      effort: "medium",
      clarifications: {},
    }),
    eventsPath: (sid) => `/api/thinking/${sid}/events`,
    cancelPath: (sid) => `/api/thinking/${sid}/cancel`,
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
    <div data-mode="thinking" class="flex h-full flex-col">
      <TopBar
        title="Thinking"
        subtitle="multi-step reasoning"
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
              Pose a question to think through
            </p>
            <p class="mt-2 text-sm text-text-tertiary">
              Thinking mode runs multi-step reasoning with streaming
              output.  Best for analysis, comparisons, and design
              decisions where you want to see the chain of thought.
            </p>
          </div>
        }
      />
      <ChatComposer
        onSubmit={stream.start}
        busy={stream.busy()}
        onCancel={stream.cancel}
        placeholder="What should I think about? (e.g. 'compare CRDT vs OT for collaborative editing')"
      />
    </div>
  );
};
