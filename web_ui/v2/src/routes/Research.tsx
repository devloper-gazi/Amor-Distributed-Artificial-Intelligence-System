import {
  type Component,
  createSignal,
  createMemo,
  onCleanup,
  Show,
} from "solid-js";
import { TopBar } from "../components/shell/TopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import { ChatComposer } from "../components/chat/ChatComposer";
import { MessageThread } from "../components/chat/MessageThread";
import { Button, StatusPill } from "../components/ui";
import { api } from "../lib/api";
import {
  openEventStream,
  type OpenedStream,
  type StreamStatus,
} from "../lib/sse";
import type { ChatTurn } from "../lib/types";

interface StartResp {
  session_id: string;
  success?: boolean;
  message?: string;
}

let _idCounter = 0;
const newId = () => `t-${Date.now()}-${++_idCounter}`;

/**
 * Research mode — single-shot autonomous research session.  The
 * pipeline is a single long-running task; SSE events report
 * progress, intermediate findings, and the final synthesis.
 */
export const Research: Component = () => {
  const [turns, setTurns] = createSignal<ChatTurn[]>([]);
  const [busy, setBusy] = createSignal(false);
  const [status, setStatus] = createSignal<StreamStatus>("closed");
  const [sessionId, setSessionId] = createSignal<string | null>(null);

  let stream: OpenedStream | null = null;
  let assistantTurnId: string | null = null;
  let buffer = "";

  const cleanup = () => {
    if (stream) {
      stream.close();
      stream = null;
    }
  };
  onCleanup(cleanup);

  const startResearch = async (topic: string) => {
    cleanup();
    buffer = "";
    setBusy(true);
    setStatus("connecting");
    setTurns((prev) => [
      ...prev,
      {
        id: newId(),
        role: "user",
        content: topic,
        ts: Date.now(),
      },
    ]);
    assistantTurnId = newId();
    setTurns((prev) => [
      ...prev,
      {
        id: assistantTurnId!,
        role: "assistant",
        content: "",
        streaming: true,
        tag: "starting…",
        ts: Date.now(),
      },
    ]);

    try {
      const resp = await api.post<StartResp>("/api/local-ai/research", {
        topic,
        depth: "medium",
      });
      setSessionId(resp.session_id);
      stream = openEventStream({
        url: `/api/local-ai/research/${resp.session_id}/events`,
        onStatusChange: (s) => setStatus(s),
        onEvent: handleEvent,
      });
    } catch (err: unknown) {
      const detail =
        (err as { body?: { detail?: string } })?.body?.detail ??
        (err instanceof Error ? err.message : "Failed to start research");
      setBusy(false);
      setStatus("closed");
      patchAssistant(`**Error:** ${String(detail)}`, "failed");
    }
  };

  const cancel = async () => {
    const sid = sessionId();
    if (!sid) {
      cleanup();
      setBusy(false);
      return;
    }
    try {
      await api.post(`/api/local-ai/research/${sid}/cancel`);
    } catch {
      // best-effort
    }
    cleanup();
    patchAssistant(buffer + "\n\n_(cancelled)_", "cancelled");
    setBusy(false);
  };

  const patchAssistant = (
    content: string,
    tag?: string,
    streaming = false,
  ) => {
    if (!assistantTurnId) return;
    const id = assistantTurnId;
    setTurns((prev) =>
      prev.map((t) =>
        t.id === id ? { ...t, content, tag, streaming } : t,
      ),
    );
  };

  const append = (chunk: string, tag?: string) => {
    buffer += chunk;
    patchAssistant(buffer, tag, true);
  };

  const handleEvent = (ev: Record<string, unknown>) => {
    const type = String(ev.type ?? "");
    switch (type) {
      case "phase_start": {
        const phase = String(ev.phase ?? ev.label ?? "");
        patchAssistant(buffer, phase ? `phase: ${phase}` : undefined, true);
        break;
      }
      case "phase_complete":
      case "phase_progress": {
        const phase = String(ev.phase ?? ev.label ?? "");
        if (phase) patchAssistant(buffer, `phase: ${phase}`, true);
        break;
      }
      case "research_chunk":
      case "synthesis_chunk":
      case "chunk":
      case "delta": {
        const text =
          typeof ev.text === "string"
            ? ev.text
            : typeof ev.content === "string"
              ? ev.content
              : "";
        if (text) append(text);
        break;
      }
      case "research_complete":
      case "deliverable_ready":
      case "complete":
      case "done": {
        const final =
          typeof ev.markdown === "string"
            ? ev.markdown
            : typeof ev.report === "string"
              ? ev.report
              : typeof ev.content === "string"
                ? ev.content
                : "";
        if (final) buffer = final;
        patchAssistant(buffer || "_done_", "done");
        cleanup();
        setBusy(false);
        break;
      }
      case "error":
      case "research_error": {
        const detail = String(ev.message ?? ev.error ?? "stream error");
        patchAssistant(buffer + `\n\n**Error:** ${detail}`, "failed");
        cleanup();
        setBusy(false);
        break;
      }
      case "cancelled":
      case "research_cancelled": {
        patchAssistant(buffer + "\n\n_(cancelled)_", "cancelled");
        cleanup();
        setBusy(false);
        break;
      }
      default:
        // Many event types we don't render explicitly are still useful
        // as activity signals — pass through if they carry text.
        if (typeof ev.text === "string") append(ev.text);
    }
  };

  const headerStatus = createMemo<"healthy" | "warming" | "warning" | "failed">(() => {
    switch (status()) {
      case "open":
        return "healthy";
      case "connecting":
      case "reconnecting":
        return "warming";
      case "offline":
        return "failed";
      default:
        return busy() ? "warming" : "healthy";
    }
  });

  return (
    <div data-mode="research" class="flex h-full flex-col">
      <TopBar
        title="Research"
        subtitle="gather, summarise, cite"
        actions={
          <Show when={busy()}>
            <StatusPill status={headerStatus()} size="sm" />
            <Button variant="secondary" size="sm" onClick={cancel}>
              Cancel
            </Button>
          </Show>
        }
      />
      <ConnectionBanner status={status()} />
      <MessageThread
        turns={turns()}
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
        onSubmit={startResearch}
        busy={busy()}
        onCancel={cancel}
        placeholder="Research topic… (e.g. 'compare CRDT vs OT for collaborative editing')"
      />
    </div>
  );
};
