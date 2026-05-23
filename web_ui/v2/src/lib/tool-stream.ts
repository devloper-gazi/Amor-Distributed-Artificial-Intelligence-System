/**
 * Cycle C Sprint 4 Day 4 — adapter from legacy AMOR SSE events to
 * the canonical Vercel AI SDK 5–compatible tool-call envelope.
 *
 * See ``docs/sse-protocol.md`` for the full mapping table and the
 * envelope shape.
 *
 * The adapter is intentionally pure — no DOM, no network, no Solid
 * primitives — so it's testable in vitest's default node environment
 * and can be reused by future native AI-SDK consumers.
 */

export type ToolEvent =
  | {
      type: "tool-input-start";
      toolCallId: string;
      tool: string;
      meta?: Record<string, unknown>;
    }
  | {
      type: "tool-input-delta";
      toolCallId: string;
      delta: string;
    }
  | {
      type: "tool-input-available";
      toolCallId: string;
      input: unknown;
    }
  | {
      type: "tool-output-available";
      toolCallId: string;
      output: unknown;
      isError?: boolean;
    }
  | {
      type: "tool-error";
      toolCallId: string;
      message: string;
    };

export interface AmorEvent {
  type: string;
  iteration?: number;
  [key: string]: unknown;
}

/**
 * Stable id for a tool call from a single legacy event.  Most AMOR
 * events come in matched start/result pairs; we key by ``tool +
 * iteration`` so a debug-retry's second sandbox call gets its own card.
 */
function callId(tool: string, ev: AmorEvent): string {
  const iter = typeof ev.iteration === "number" ? ev.iteration : 0;
  return `${tool}-${iter}`;
}

const TRACK_TOOLS = {
  execution_start: "sandbox-execute",
  execution_install_packages: "sandbox-execute",
  execution_extra_files: "sandbox-execute",
  execution_result: "sandbox-execute",
  static_analysis_result: "static-analysis",
  review_ready: "code-review",
  repomap_attached: "repomap-attach",
  test_ready: "test-author",
  language_corrected: "language-detect",
  debug_iteration_start: "debug-retry",
  model_download_start: "model-pull",
  model_download_progress: "model-pull",
  model_download_complete: "model-pull",
} as const;

/**
 * Project a legacy AMOR pipeline event into 0..N canonical
 * tool-stream events.  Returns an empty array when the input event
 * doesn't correspond to a tool call (e.g. ``phase_start``).
 */
export function toToolEvents(ev: AmorEvent): ToolEvent[] {
  // Already canonical → pass-through.
  if (ev.type.startsWith("tool-")) {
    return [ev as unknown as ToolEvent];
  }

  const tool = (TRACK_TOOLS as Record<string, string>)[ev.type];
  if (!tool) return [];
  const id = callId(tool, ev);

  switch (ev.type) {
    case "execution_start": {
      return [
        {
          type: "tool-input-start",
          toolCallId: id,
          tool,
          meta: {
            language: ev.language,
            iteration: ev.iteration ?? 0,
          },
        },
      ];
    }
    case "execution_install_packages": {
      return [
        {
          type: "tool-input-delta",
          toolCallId: id,
          delta: JSON.stringify({ packages: ev.packages ?? [] }),
        },
      ];
    }
    case "execution_extra_files": {
      const files = ev.files as Record<string, unknown> | undefined;
      return [
        {
          type: "tool-input-delta",
          toolCallId: id,
          delta: JSON.stringify({
            extra_files_count: files ? Object.keys(files).length : 0,
          }),
        },
      ];
    }
    case "execution_result": {
      const exitCode = (ev.exit_code as number | undefined) ?? 0;
      return [
        {
          type: "tool-input-available",
          toolCallId: id,
          input: { language: ev.language, iteration: ev.iteration ?? 0 },
        },
        {
          type: "tool-output-available",
          toolCallId: id,
          output: {
            exit_code: exitCode,
            stdout: ev.stdout,
            stderr: ev.stderr,
            duration_ms: ev.duration_ms,
          },
          isError: exitCode !== 0,
        },
      ];
    }
    case "static_analysis_result": {
      return [
        {
          type: "tool-input-start",
          toolCallId: id,
          tool,
          meta: { iteration: ev.iteration ?? 0 },
        },
        {
          type: "tool-output-available",
          toolCallId: id,
          output: {
            findings: ev.findings,
            ok: ev.ok,
          },
          isError: ev.ok === false,
        },
      ];
    }
    case "review_ready": {
      return [
        {
          type: "tool-input-start",
          toolCallId: id,
          tool,
          meta: { iteration: ev.iteration ?? 0 },
        },
        {
          type: "tool-output-available",
          toolCallId: id,
          output: {
            score: ev.score,
            summary: ev.summary,
            findings: ev.findings,
          },
        },
      ];
    }
    case "repomap_attached": {
      return [
        {
          type: "tool-output-available",
          toolCallId: id,
          output: {
            tokens_estimate: ev.tokens_estimate,
            render_ms: ev.render_ms,
            budget_tokens: ev.budget_tokens,
          },
        },
      ];
    }
    case "test_ready": {
      return [
        {
          type: "tool-input-start",
          toolCallId: id,
          tool,
          meta: { iteration: ev.iteration ?? 0 },
        },
        {
          type: "tool-output-available",
          toolCallId: id,
          output: { tests_present: true },
        },
      ];
    }
    case "language_corrected": {
      return [
        {
          type: "tool-output-available",
          toolCallId: id,
          output: { from: ev.from, to: ev.to, reason: ev.reason },
        },
      ];
    }
    case "debug_iteration_start": {
      return [
        {
          type: "tool-input-start",
          toolCallId: id,
          tool,
          meta: { iteration: ev.iteration ?? 0 },
        },
      ];
    }
    case "model_download_start": {
      return [
        {
          type: "tool-input-start",
          toolCallId: id,
          tool,
          meta: { tag: ev.tag },
        },
      ];
    }
    case "model_download_progress": {
      return [
        {
          type: "tool-input-delta",
          toolCallId: id,
          delta: JSON.stringify({ percent: ev.percent ?? null }),
        },
      ];
    }
    case "model_download_complete": {
      return [
        {
          type: "tool-output-available",
          toolCallId: id,
          output: { tag: ev.tag, ok: ev.ok ?? true },
          isError: ev.ok === false,
        },
      ];
    }
    default:
      return [];
  }
}

/**
 * In-memory accumulator that turns a stream of ``ToolEvent``s into
 * a render-ready ``ToolCallFrame`` per ``toolCallId``.  ``ingest``
 * mutates the current map and returns the updated value so callers
 * can drive a Solid signal with it.
 */
export interface ToolCallFrame {
  id: string;
  tool: string;
  status: "pending" | "running" | "complete" | "error";
  inputDelta: string;
  input?: unknown;
  output?: unknown;
  isError?: boolean;
  meta?: Record<string, unknown>;
  errorMessage?: string;
}

export function ingestToolEvent(
  prev: Map<string, ToolCallFrame>,
  ev: ToolEvent,
): Map<string, ToolCallFrame> {
  const next = new Map(prev);
  const existing = next.get(ev.toolCallId);
  switch (ev.type) {
    case "tool-input-start":
      next.set(ev.toolCallId, {
        id: ev.toolCallId,
        tool: ev.tool,
        status: "running",
        inputDelta: "",
        meta: ev.meta,
      });
      break;
    case "tool-input-delta":
      if (!existing) break;
      next.set(ev.toolCallId, {
        ...existing,
        status: existing.status === "complete" ? "complete" : "running",
        inputDelta: existing.inputDelta + ev.delta,
      });
      break;
    case "tool-input-available":
      if (!existing) {
        next.set(ev.toolCallId, {
          id: ev.toolCallId,
          tool: "(unknown)",
          status: "running",
          inputDelta: "",
          input: ev.input,
        });
      } else {
        next.set(ev.toolCallId, {
          ...existing,
          status: "running",
          input: ev.input,
        });
      }
      break;
    case "tool-output-available":
      if (!existing) {
        next.set(ev.toolCallId, {
          id: ev.toolCallId,
          tool: "(unknown)",
          status: ev.isError ? "error" : "complete",
          inputDelta: "",
          output: ev.output,
          isError: ev.isError,
        });
      } else {
        next.set(ev.toolCallId, {
          ...existing,
          status: ev.isError ? "error" : "complete",
          output: ev.output,
          isError: ev.isError,
        });
      }
      break;
    case "tool-error":
      if (!existing) {
        next.set(ev.toolCallId, {
          id: ev.toolCallId,
          tool: "(unknown)",
          status: "error",
          inputDelta: "",
          errorMessage: ev.message,
        });
      } else {
        next.set(ev.toolCallId, {
          ...existing,
          status: "error",
          errorMessage: ev.message,
        });
      }
      break;
  }
  return next;
}
