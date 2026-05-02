import { type Component, createMemo, Show } from "solid-js";
import { TopBar } from "../components/shell/TopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import { ChatComposer } from "../components/chat/ChatComposer";
import { MessageThread } from "../components/chat/MessageThread";
import { Button, StatusPill, type Status } from "../components/ui";
import {
  getChatStream,
  SIMPLE_TEXT_REDUCER,
  type EventReducer,
} from "../lib/chat-stream";

interface SentinelRequest {
  prompt: string;
  paths: string[];
  code_context: string;
}

/**
 * Sentinel mode — security + governance scan.  Multi-agent swarm
 * over a 7-layer ML pipeline.  Same chat shape as the others;
 * outputs a SARIF-shaped summary at the end.
 */
export const Sentinel: Component = () => {
  const reducer: EventReducer = (ev) => {
    const patch = SIMPLE_TEXT_REDUCER(ev);
    if (!patch) return null;
    if (patch.tag) {
      patch.tag = patch.tag.replace(/^phase:\s*sentinel_/, "phase: ");
    }
    return patch;
  };

  const stream = getChatStream<SentinelRequest>({
    startPath: "/api/sentinel/start",
    buildStartBody: (prompt) => ({
      prompt,
      paths: [],
      code_context: "",
    }),
    eventsPath: (sid) => `/api/sentinel/${sid}/events`,
    cancelPath: (sid) => `/api/sentinel/${sid}/cancel`,
    reduce: reducer,
  });

  const headerStatus = createMemo<Status>(() => {
    const s = stream.status();
    if (s === "open") return stream.busy() ? "warming" : "healthy";
    if (s === "connecting" || s === "reconnecting") return "warming";
    if (s === "offline") return "failed";
    return stream.busy() ? "warming" : "healthy";
  });

  return (
    <div data-mode="sentinel" class="flex h-full flex-col">
      <TopBar
        title="Sentinel"
        subtitle="governance, ledger, evolution"
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
              Describe what to audit
            </p>
            <p class="mt-2 text-sm text-text-tertiary">
              Sentinel runs a multi-agent security + governance scan.
              Pass a prompt describing the surface (codebase area,
              session id, deliverable) and AMOR will audit + emit a
              SARIF-shaped report.
            </p>
          </div>
        }
      />
      <ChatComposer
        onSubmit={stream.start}
        busy={stream.busy()}
        onCancel={stream.cancel}
        placeholder="What should Sentinel audit? (e.g. 'check the auth middleware for CSRF + XSS')"
      />
    </div>
  );
};
