import {
  type Component,
  createSignal,
  createMemo,
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
  type SseEvent,
} from "../lib/sse";
import type { ChatTurn } from "../lib/types";

interface StartResp {
  session_id: string;
  success?: boolean;
}

interface PhaseDef {
  key: string;
  label: string;
  pct: number;
  /** User-facing description shown while this phase is running so
   *  the chat thread isn't a blank "starting…" for 1–2 minutes
   *  during a slow LLM phase like implement. */
  doingNow: string;
}

const PHASES: ReadonlyArray<PhaseDef> = [
  { key: "triage",     label: "Triage",      pct: 10, doingNow: "Classifying the request" },
  { key: "model_prep", label: "Model prep",  pct: 15, doingNow: "Preparing models" },
  { key: "plan",       label: "Plan",        pct: 25, doingNow: "Drafting a plan" },
  { key: "implement",  label: "Implement",   pct: 50, doingNow: "Writing the code (this is the slow phase — usually 30–120 s)" },
  { key: "execute",    label: "Execute",     pct: 60, doingNow: "Running the code in the sandbox" },
  { key: "analyze",    label: "Analyse",     pct: 68, doingNow: "Static-analysing the output" },
  { key: "test",       label: "Test",        pct: 78, doingNow: "Generating tests" },
  { key: "debug",      label: "Debug",       pct: 88, doingNow: "Debugging failures" },
  { key: "review",     label: "Review",      pct: 98, doingNow: "Final review" },
];

const PHASE_BY_KEY: Record<string, PhaseDef> = Object.fromEntries(
  PHASES.map((p) => [p.key, p]),
);

type PhaseStatus = "pending" | "running" | "done" | "failed" | "skipped";

let _idCounter = 0;
const newId = (): string => `b-${Date.now()}-${++_idCounter}`;

/* ─── Module-scoped state ──────────────────────────────────────────
 * Signals created at module level so the user can navigate away
 * from /build (e.g. to /system) and come back without wiping the
 * conversation or killing an in-flight pipeline.  ``resetBuild``
 * clears state on logout. */

const [turns, setTurns] = createSignal<ChatTurn[]>([]);
const [busy, setBusy] = createSignal(false);
const [status, setStatus] = createSignal<StreamStatus>("closed");
const [sessionId, setSessionId] = createSignal<string | null>(null);
const [phases, setPhases] = createSignal<Record<string, PhaseStatus>>({});
/** Active phase key + when it started.  Drives the live status block
 *  rendered above the composer so the user doesn't stare at a blank
 *  "starting…" for 60+ seconds during a slow phase. */
const [activePhase, setActivePhase] = createSignal<string | null>(null);
const [phaseStartedAt, setPhaseStartedAt] = createSignal<number | null>(null);
/** Re-renders every second so the elapsed counter ticks live. */
const [tickNow, setTickNow] = createSignal<number>(Date.now());

let stream: OpenedStream | null = null;
let assistantTurnId: string | null = null;
let tickTimer: ReturnType<typeof setInterval> | null = null;

const startTicker = (): void => {
  if (tickTimer !== null) return;
  tickTimer = setInterval(() => setTickNow(Date.now()), 1000);
};
const stopTicker = (): void => {
  if (tickTimer !== null) {
    clearInterval(tickTimer);
    tickTimer = null;
  }
};

const cleanupStream = (): void => {
  if (stream) {
    stream.close();
    stream = null;
  }
};

export function resetBuild(): void {
  cleanupStream();
  stopTicker();
  setTurns([]);
  setBusy(false);
  setStatus("closed");
  setSessionId(null);
  setPhases({});
  setActivePhase(null);
  setPhaseStartedAt(null);
  assistantTurnId = null;
}

const setPhase = (key: string, st: PhaseStatus): void => {
  setPhases((prev) => ({ ...prev, [key]: st }));
};

const patchAssistant = (
  content: string,
  tag?: string,
  streaming = false,
): void => {
  if (!assistantTurnId) return;
  const id = assistantTurnId;
  setTurns((prev) =>
    prev.map((t) => (t.id === id ? { ...t, content, tag, streaming } : t)),
  );
};

const currentBuffer = (): string => {
  if (!assistantTurnId) return "";
  const t = turns().find((x) => x.id === assistantTurnId);
  return t?.content ?? "";
};

const appendBlock = (block: string): void => {
  const cur = currentBuffer();
  const next =
    cur === "_(starting…)_" || cur === "" ? block : cur + "\n" + block;
  patchAssistant(next, undefined, true);
};

const handleEvent = (ev: SseEvent): void => {
  const type = String(ev.type ?? "");
  const phase = String(ev.phase ?? "");
  switch (type) {
    case "phase_start":
      if (phase) {
        setPhase(phase, "running");
        setActivePhase(phase);
        setPhaseStartedAt(Date.now());
        startTicker();
      }
      patchAssistant(currentBuffer(), `phase: ${phase}`, true);
      break;
    case "phase_complete":
      if (phase) {
        setPhase(phase, "done");
        if (activePhase() === phase) setActivePhase(null);
      }
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
      const summary = String(review.final_comment ?? review.summary ?? "");
      appendBlock(`### Review\n\n**Verdict:** ${verdict}\n\n${summary}\n`);
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
      patchAssistant(currentBuffer(), `pulling ${model} ${pct}%`, true);
      break;
    }
    case "done":
      for (const p of PHASES) {
        if (!phases()[p.key]) setPhase(p.key, "done");
      }
      patchAssistant(currentBuffer() || "_(done)_", "done");
      cleanupStream();
      stopTicker();
      setActivePhase(null);
      setBusy(false);
      break;
    case "error":
      patchAssistant(
        currentBuffer() +
          `\n\n**Error:** ${String(ev.message ?? "unknown")}`,
        "failed",
      );
      cleanupStream();
      stopTicker();
      setActivePhase(null);
      setBusy(false);
      break;
    case "cancelled":
      patchAssistant(currentBuffer() + "\n\n_(cancelled)_", "cancelled");
      cleanupStream();
      stopTicker();
      setActivePhase(null);
      setBusy(false);
      break;
  }
};

const start = async (prompt: string): Promise<void> => {
  cleanupStream();
  setBusy(true);
  setStatus("connecting");
  setPhases({});
  setTurns((prev) => [
    ...prev,
    { id: newId(), role: "user", content: prompt, ts: Date.now() },
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

const cancel = async (): Promise<void> => {
  const sid = sessionId();
  if (sid) {
    try {
      await api.post(`/api/code/${sid}/cancel`);
    } catch {
      // ignore
    }
  }
  cleanupStream();
  stopTicker();
  setActivePhase(null);
  patchAssistant("_(cancelled)_", "cancelled");
  setBusy(false);
};

/**
 * Live phase status bar — rendered above the composer while busy.
 * Shows the current phase's user-friendly description + an elapsed
 * counter that ticks every second.  Empty when the pipeline isn't
 * running.  Solves the "(starting…) for 60 s" black hole during
 * the slow Implement phase.
 */
const PhaseStatusBar: Component = () => {
  const elapsed = createMemo<number>(() => {
    const start = phaseStartedAt();
    if (start === null) return 0;
    return Math.max(0, Math.floor((tickNow() - start) / 1000));
  });

  const fmtElapsed = (s: number): string => {
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}m ${r.toString().padStart(2, "0")}s`;
  };

  return (
    <Show when={busy() && activePhase()}>
      {(phase) => {
        const def = (): PhaseDef | undefined => PHASE_BY_KEY[phase()];
        return (
          <div
            role="status"
            aria-live="polite"
            class="flex items-center gap-3 border-t border-border-subtle bg-bg-secondary px-5 py-3 text-sm"
          >
            <span
              class="h-2 w-2 rounded-full motion-safe:animate-pulse"
              style={{ background: "var(--mode-accent)" }}
              aria-hidden="true"
            />
            <span class="flex-1 truncate text-text-primary">
              <span class="font-medium">{def()?.label ?? phase()}</span>
              <span class="ml-2 text-text-secondary">
                {def()?.doingNow ?? "running"}
              </span>
            </span>
            <span class="font-mono text-xs text-text-tertiary tabular-nums">
              {fmtElapsed(elapsed())}
            </span>
          </div>
        );
      }}
    </Show>
  );
};

/**
 * Build mode component — pure render shell.  All state lives at the
 * module level above so it survives route remounts (Build → System
 * → Build doesn't wipe an in-flight pipeline).
 */
export const Build: Component = () => {
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
        <PhaseStatusBar />
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
