import {
  type Component,
  createSignal,
  createMemo,
  onCleanup,
  Show,
  For,
} from "solid-js";
import { TopBar } from "../components/shell/TopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import { ChatComposer } from "../components/chat/ChatComposer";
import { MessageThread } from "../components/chat/MessageThread";
import {
  Button,
  StatusPill,
  type Status,
} from "../components/ui";
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
}

const PHASES: ReadonlyArray<{
  key: string;
  label: string;
  pct: number;
}> = [
  { key: "triage", label: "Triage", pct: 10 },
  { key: "model_prep", label: "Model prep", pct: 15 },
  { key: "plan", label: "Plan", pct: 25 },
  { key: "implement", label: "Implement", pct: 50 },
  { key: "execute", label: "Execute", pct: 60 },
  { key: "analyze", label: "Analyse", pct: 68 },
  { key: "test", label: "Test", pct: 78 },
  { key: "debug", label: "Debug", pct: 88 },
  { key: "review", label: "Review", pct: 98 },
];

type PhaseStatus = "pending" | "running" | "done" | "failed" | "skipped";

let _idCounter = 0;
const newId = () => `b-${Date.now()}-${++_idCounter}`;

/**
 * Build mode — Code Intelligence pipeline.  9 phases visualised in
 * a left rail, conversation in the centre.  Each event from the
 * backend SSE stream updates phase state + appends to the
 * assistant turn when it carries content.
 */
export const Build: Component = () => {
  const [turns, setTurns] = createSignal<ChatTurn[]>([]);
  const [busy, setBusy] = createSignal(false);
  const [status, setStatus] = createSignal<StreamStatus>("closed");
  const [sessionId, setSessionId] = createSignal<string | null>(null);
  const [phases, setPhases] = createSignal<Record<string, PhaseStatus>>({});

  let stream: OpenedStream | null = null;
  let assistantTurnId: string | null = null;

  const cleanup = () => {
    if (stream) {
      stream.close();
      stream = null;
    }
  };
  onCleanup(cleanup);

  const setPhase = (key: string, st: PhaseStatus) => {
    setPhases((prev) => ({ ...prev, [key]: st }));
  };

  const start = async (prompt: string) => {
    cleanup();
    setBusy(true);
    setStatus("connecting");
    setPhases({});
    setTurns((prev) => [
      ...prev,
      {
        id: newId(),
        role: "user",
        content: prompt,
        ts: Date.now(),
      },
    ]);
    assistantTurnId = newId();
    setTurns((prev) => [
      ...prev,
      {
        id: assistantTurnId!,
        role: "assistant",
        content: "_(starting…)_",
        streaming: true,
        tag: "phase: triage",
        ts: Date.now(),
      },
    ]);

    try {
      const resp = await api.post<StartResp>("/api/code/start", {
        prompt,
        effort: "medium",
      });
      setSessionId(resp.session_id);
      stream = openEventStream({
        url: `/api/code/${resp.session_id}/events`,
        onStatusChange: (s) => setStatus(s),
        onEvent: handleEvent,
      });
    } catch (err: unknown) {
      const detail =
        (err as { body?: { detail?: string } })?.body?.detail ??
        (err instanceof Error ? err.message : "Failed to start build");
      setBusy(false);
      setStatus("closed");
      patchAssistant(`**Error:** ${String(detail)}`, "failed");
    }
  };

  const cancel = async () => {
    const sid = sessionId();
    if (sid) {
      try {
        await api.post(`/api/code/${sid}/cancel`);
      } catch {
        // ignore
      }
    }
    cleanup();
    patchAssistant("_(cancelled)_", "cancelled");
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

  const handleEvent = (ev: Record<string, unknown>) => {
    const type = String(ev.type ?? "");
    const phase = String(ev.phase ?? "");
    switch (type) {
      case "phase_start":
        if (phase) setPhase(phase, "running");
        patchAssistant(currentBuffer(), `phase: ${phase}`, true);
        break;
      case "phase_complete":
        if (phase) setPhase(phase, "done");
        break;
      case "phase_failed":
        if (phase) setPhase(phase, "failed");
        patchAssistant(
          currentBuffer() +
            `\n\n**Phase failed:** ${phase} — ${String(ev.error ?? "")}`,
          "failed",
        );
        break;
      case "code_ready": {
        const code = String(ev.code ?? "");
        const lang = String(ev.language ?? "");
        appendBlock(`### Code (${lang})\n\n\`\`\`${lang}\n${code}\n\`\`\`\n`);
        break;
      }
      case "test_ready": {
        const code = String(ev.code ?? "");
        appendBlock(`### Tests\n\n\`\`\`\n${code}\n\`\`\`\n`);
        break;
      }
      case "execution_result": {
        const result = (ev.result ?? {}) as Record<string, unknown>;
        const stdout = String(result.stdout ?? "").trim();
        const stderr = String(result.stderr ?? "").trim();
        const exit = result.exit_code;
        const skipped = Boolean(result.skipped);
        const block = [
          `### Execution${skipped ? " (skipped)" : ""}`,
          exit !== undefined ? `exit_code: ${exit}` : "",
          stdout
            ? `\n**stdout:**\n\n\`\`\`\n${stdout.slice(0, 2000)}\n\`\`\``
            : "",
          stderr
            ? `\n**stderr:**\n\n\`\`\`\n${stderr.slice(0, 1000)}\n\`\`\``
            : "",
        ]
          .filter(Boolean)
          .join("\n");
        appendBlock(block + "\n");
        break;
      }
      case "review_ready": {
        const review = (ev.review ?? ev.detail ?? {}) as Record<string, unknown>;
        const verdict = String(review.verdict ?? review.score ?? "—");
        const summary = String(
          review.final_comment ?? review.summary ?? "",
        );
        appendBlock(
          `### Review\n\n**Verdict:** ${verdict}\n\n${summary}\n`,
        );
        break;
      }
      case "deliverable_ready": {
        const md = String(ev.markdown ?? "");
        if (md) appendBlock(`### Deliverable\n\n${md}\n`);
        break;
      }
      case "model_download_progress": {
        const pct = Number(ev.pct ?? 0);
        const model = String(ev.model ?? "");
        patchAssistant(
          currentBuffer(),
          `pulling ${model} ${pct}%`,
          true,
        );
        break;
      }
      case "done":
        for (const p of PHASES) {
          if (!phases()[p.key]) setPhase(p.key, "done");
        }
        patchAssistant(currentBuffer() || "_(done)_", "done");
        cleanup();
        setBusy(false);
        break;
      case "error":
        patchAssistant(
          currentBuffer() +
            `\n\n**Error:** ${String(ev.message ?? "unknown")}`,
          "failed",
        );
        cleanup();
        setBusy(false);
        break;
      case "cancelled":
        patchAssistant(currentBuffer() + "\n\n_(cancelled)_", "cancelled");
        cleanup();
        setBusy(false);
        break;
    }
  };

  const currentBuffer = (): string => {
    if (!assistantTurnId) return "";
    const t = turns().find((x) => x.id === assistantTurnId);
    return t?.content ?? "";
  };

  const appendBlock = (block: string) => {
    const cur = currentBuffer();
    const next =
      cur === "_(starting…)_" || cur === ""
        ? block
        : cur + "\n" + block;
    patchAssistant(next, undefined, true);
  };

  const headerStatus = createMemo<Status>(() => {
    switch (status()) {
      case "open":
        return busy() ? "warming" : "healthy";
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
    <div data-mode="build" class="flex h-full">
      {/* Left rail — phase timeline */}
      <aside class="hidden w-52 shrink-0 border-r border-border-subtle bg-bg-secondary lg:flex lg:flex-col">
        <div class="border-b border-border-subtle px-3 py-3 text-[0.65rem] font-semibold uppercase tracking-widest text-text-tertiary">
          Pipeline
        </div>
        <ol class="flex-1 overflow-y-auto p-2 space-y-1">
          <For each={PHASES}>
            {(p) => {
              const st = (): PhaseStatus => phases()[p.key] ?? "pending";
              const dot = (): string => {
                switch (st()) {
                  case "running":
                    return "●";
                  case "done":
                    return "✓";
                  case "failed":
                    return "✗";
                  case "skipped":
                    return "○";
                  default:
                    return "○";
                }
              };
              return (
                <li
                  class={[
                    "flex items-center gap-2 rounded-md px-2 py-1.5 text-xs",
                    st() === "running"
                      ? "bg-bg-hover text-text-primary"
                      : st() === "done"
                        ? "text-text-secondary"
                        : st() === "failed"
                          ? "text-status-failed"
                          : "text-text-tertiary",
                  ].join(" ")}
                >
                  <span
                    class={[
                      "w-3 text-center",
                      st() === "running"
                        ? "motion-safe:animate-pulse"
                        : "",
                    ].join(" ")}
                    aria-hidden="true"
                    style={
                      st() === "running"
                        ? { color: "var(--mode-accent)" }
                        : undefined
                    }
                  >
                    {dot()}
                  </span>
                  <span class="flex-1 truncate">{p.label}</span>
                  <span class="text-[0.6rem] text-text-tertiary">
                    {p.pct}%
                  </span>
                </li>
              );
            }}
          </For>
        </ol>
      </aside>

      {/* Right side — chat */}
      <div class="flex min-w-0 flex-1 flex-col">
        <TopBar
          title="Build"
          subtitle="code, test, debug"
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
                Describe what you want built
              </p>
              <p class="mt-2 text-sm text-text-tertiary">
                Plan → Implement → Execute → Analyse → Test → Debug
                → Review.  Code Intelligence runs the full pipeline
                with live updates here.
              </p>
            </div>
          }
        />
        <ChatComposer
          onSubmit={start}
          busy={busy()}
          onCancel={cancel}
          placeholder="What should AMOR build? (e.g. 'fizzbuzz with pytest')"
        />
      </div>
    </div>
  );
};
