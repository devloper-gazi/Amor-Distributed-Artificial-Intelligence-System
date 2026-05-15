/**
 * Cycle C Sprint 8 Day 4 — agentic ReAct loop UI.
 *
 * Pipes the SSE stream from ``/api/agent/sessions/<sid>/events`` into
 * the Sprint 4 Day 4 ``ToolCallCard`` accumulator + a thought log.
 * The user types a task, hits Run, and watches the agent think /
 * call tools / observe in real time.
 *
 * Layout
 * ------
 *   ┌─────────────────────────────┐
 *   │ Composer (single textarea)  │
 *   ├─────────────────────────────┤
 *   │ Thought timeline            │
 *   │ Tool call cards             │
 *   │ Final answer + reason       │
 *   └─────────────────────────────┘
 */

import {
  type Component,
  For,
  Show,
  createMemo,
  createSignal,
  onCleanup,
} from "solid-js";

import { TopBar } from "../components/shell/TopBar";
import { Badge, Button, Spinner, Textarea } from "../components/ui";
import { ToolCallCard } from "../components/chat/ToolCallCard";
import {
  ingestToolEvent,
  type ToolCallFrame,
  type ToolEvent,
} from "../lib/tool-stream";
import { api } from "../lib/api";
import { t } from "../i18n";


interface AgentEvent {
  id: string;
  ts_iso: string;
  kind: "message" | "thought" | "action" | "observation";
  meta?: Record<string, unknown>;
  // Discriminated tail — Pydantic serialises every field in the
  // active subclass; the front-end picks what it needs.
  role?: "user" | "assistant";
  text?: string;
  iteration?: number;
  tool?: string;
  arguments?: Record<string, unknown>;
  output?: unknown;
  is_error?: boolean;
  error_message?: string | null;
  elapsed_ms?: number;
}

interface AgentEnvelope {
  type:
    | "agent.snapshot"
    | "agent.event"
    | "agent.done"
    | "agent.cancelled"
    | "agent.error";
  // .snapshot
  iteration?: number;
  finished?: boolean;
  finish_reason?: string | null;
  events?: AgentEvent[];
  // .event
  event?: AgentEvent;
  tool_stream?: ToolEvent[];
  // .done
  reason?: "finish" | "max-iterations" | "stuck" | "parse-failure";
  answer?: string | null;
  iterations?: number;
  // .error
  message?: string;
}


export const Agent: Component = () => {
  const [task, setTask] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  const [sid, setSid] = createSignal<string | null>(null);
  const [thoughts, setThoughts] = createSignal<AgentEvent[]>([]);
  const [frames, setFrames] = createSignal<Map<string, ToolCallFrame>>(new Map());
  const [finalAnswer, setFinalAnswer] = createSignal<string | null>(null);
  const [finishReason, setFinishReason] = createSignal<string | null>(null);
  const [errorMsg, setErrorMsg] = createSignal<string | null>(null);
  let activeStream: EventSource | null = null;

  const thoughtCount = createMemo(() => thoughts().length);
  const frameList = createMemo(() => Array.from(frames().values()));

  onCleanup(() => {
    if (activeStream) activeStream.close();
  });

  const reset = () => {
    if (activeStream) {
      activeStream.close();
      activeStream = null;
    }
    setSid(null);
    setThoughts([]);
    setFrames(new Map());
    setFinalAnswer(null);
    setFinishReason(null);
    setErrorMsg(null);
    setBusy(false);
  };

  const handleEnvelope = (env: AgentEnvelope) => {
    if (env.type === "agent.snapshot") {
      // Replay thoughts + tool stream from prior events so a refresh
      // mid-run doesn't drop history.
      const allThoughts = (env.events ?? []).filter((e) => e.kind === "thought");
      setThoughts(allThoughts);
      let m = new Map<string, ToolCallFrame>();
      for (const e of env.events ?? []) {
        if (e.kind === "action" || e.kind === "observation") {
          // Reconstruct a synthetic ToolEvent stream from the snapshot.
          const cid = `${e.tool ?? "?"}-${e.iteration ?? 0}`;
          if (e.kind === "action") {
            m = ingestToolEvent(m, {
              type: "tool-input-start",
              toolCallId: cid,
              tool: e.tool ?? "?",
              meta: { iteration: e.iteration ?? 0 },
            });
            m = ingestToolEvent(m, {
              type: "tool-input-available",
              toolCallId: cid,
              input: e.arguments ?? {},
            });
          } else {
            m = ingestToolEvent(m, {
              type: "tool-output-available",
              toolCallId: cid,
              output: e.output,
              isError: !!e.is_error,
            });
          }
        }
      }
      setFrames(m);
      if (env.finished) {
        setFinishReason(env.finish_reason ?? "done");
        setBusy(false);
      }
      return;
    }
    if (env.type === "agent.event" && env.event) {
      const ev = env.event;
      if (ev.kind === "thought") {
        setThoughts((prev) => [...prev, ev]);
      }
      if ((env.tool_stream ?? []).length > 0) {
        setFrames((prev) => {
          let m = prev;
          for (const tev of env.tool_stream!) m = ingestToolEvent(m, tev);
          return m;
        });
      }
      return;
    }
    if (env.type === "agent.done") {
      setFinishReason(env.reason ?? "done");
      setFinalAnswer(env.answer ?? null);
      setBusy(false);
      return;
    }
    if (env.type === "agent.cancelled") {
      setFinishReason("cancelled");
      setBusy(false);
      return;
    }
    if (env.type === "agent.error") {
      setErrorMsg(env.message ?? "agent error");
      setBusy(false);
    }
  };

  const start = async () => {
    if (busy()) return;
    const text = task().trim();
    if (!text) return;
    reset();
    setBusy(true);
    try {
      const resp = await api.post<{ session_id: string }>(
        "/api/agent/start",
        { task: text, max_iterations: 10, stuck_window: 3 },
      );
      setSid(resp.session_id);
      // SSE — credentials needed so the auth cookie rides along.
      activeStream = new EventSource(
        `/api/agent/sessions/${encodeURIComponent(resp.session_id)}/events`,
        { withCredentials: true },
      );
      activeStream.onmessage = (e) => {
        try {
          handleEnvelope(JSON.parse(e.data) as AgentEnvelope);
        } catch {
          // Ignore malformed frames; the keep-alive pings are not JSON.
        }
      };
      activeStream.onerror = () => {
        // Browser auto-reconnects on transient errors; only flip
        // busy off when we've actually finished.
        if (!finishReason()) {
          setErrorMsg("connection interrupted — reconnecting…");
        }
      };
    } catch (err) {
      setErrorMsg(
        (err as { body?: { detail?: string }; message?: string })?.body?.detail
          ?? (err as Error).message
          ?? "start failed",
      );
      setBusy(false);
    }
  };

  const cancel = async () => {
    const s = sid();
    if (!s) return;
    try {
      await api.post(`/api/agent/sessions/${encodeURIComponent(s)}/cancel`);
    } catch {
      // best-effort
    }
  };

  return (
    <div data-mode="system" class="flex h-full flex-col">
      <TopBar
        title={t("agent.title")}
        subtitle={t("agent.subtitle")}
        actions={
          <>
            <Show when={busy()}>
              <Spinner size={14} />
            </Show>
            <Show when={sid() && !busy()}>
              <Button variant="secondary" size="sm" onClick={reset}>
                {t("agent.new_run")}
              </Button>
            </Show>
          </>
        }
      />

      <div class="flex-1 overflow-y-auto px-6 py-6">
        <div class="mx-auto max-w-3xl space-y-4">
          <section class="space-y-2">
            <Textarea
              value={task()}
              onInput={(e: InputEvent & { currentTarget: HTMLTextAreaElement }) =>
                setTask(e.currentTarget.value)
              }
              minRows={2}
              maxRows={6}
              placeholder={t("agent.composer.placeholder")}
              aria-label={t("agent.composer.aria")}
              disabled={busy()}
            />
            <div class="flex items-center gap-2">
              <Button
                onClick={start}
                disabled={busy() || !task().trim()}
              >
                {busy() ? t("agent.running") : t("agent.run")}
              </Button>
              <Show when={busy() && sid()}>
                <Button variant="secondary" size="sm" onClick={cancel}>
                  {t("agent.cancel")}
                </Button>
              </Show>
              <Show when={sid()}>
                <code class="text-xs text-text-tertiary">
                  sid {sid()!.slice(0, 8)}
                </code>
              </Show>
              <Show when={finishReason()}>
                <Badge>{finishReason()}</Badge>
              </Show>
              <Show when={thoughtCount() > 0}>
                <span class="text-xs text-text-tertiary">
                  {thoughtCount()} thought{thoughtCount() === 1 ? "" : "s"}
                </span>
              </Show>
            </div>
          </section>

          <Show when={errorMsg()}>
            <div
              role="alert"
              class="rounded-md border border-status-error/40 bg-status-error/10 px-3 py-2 text-xs text-status-error"
            >
              {errorMsg()}
            </div>
          </Show>

          <Show when={thoughtCount() > 0}>
            <section class="space-y-2">
              <h2 class="text-sm font-semibold tracking-tight">
                {t("agent.thoughts.heading")}
              </h2>
              <ul class="space-y-1.5">
                <For each={thoughts()}>
                  {(turn) => (
                    <li class="rounded-md border border-border-subtle bg-bg-elevated px-3 py-2 text-xs text-text-secondary">
                      <span class="text-[0.65rem] uppercase tracking-wide text-text-tertiary">
                        {t("toolcall.iteration", { n: String(turn.iteration ?? "?") })}
                      </span>
                      <p class="mt-0.5 whitespace-pre-wrap">{turn.text}</p>
                    </li>
                  )}
                </For>
              </ul>
            </section>
          </Show>

          <Show when={frameList().length > 0}>
            <section class="space-y-2">
              <h2 class="text-sm font-semibold tracking-tight">
                {t("agent.tool_calls.heading")}
              </h2>
              <div class="space-y-2">
                <For each={frameList()}>
                  {(frame) => <ToolCallCard frame={frame} />}
                </For>
              </div>
            </section>
          </Show>

          <Show when={finalAnswer()}>
            <section class="rounded-md border border-border-subtle bg-bg-elevated p-3">
              <h2 class="text-sm font-semibold tracking-tight">
                {t("agent.answer.heading")}
              </h2>
              <p class="mt-1 whitespace-pre-wrap text-sm text-text-primary">
                {finalAnswer()}
              </p>
            </section>
          </Show>

          <Show when={!sid() && !errorMsg()}>
            <p class="text-xs text-text-tertiary">
              {t("agent.empty_intro")}
            </p>
          </Show>
        </div>
      </div>
    </div>
  );
};
