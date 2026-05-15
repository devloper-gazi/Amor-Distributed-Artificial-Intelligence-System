/**
 * Cycle C Sprint 4 Day 4 — animated tool-call card.
 *
 * Renders one ``ToolCallFrame`` (see ``../../lib/tool-stream.ts``) with
 * the canonical pending → running → complete | error state machine.
 * Input + output payloads live behind a native ``<details>`` so the
 * default rendered surface is small.
 *
 * Visual conventions
 * ------------------
 * * 2 px mode-accent left border — keeps the card threaded with the
 *   surrounding mode (Build/Research/etc.).
 * * Status pill: spinner while ``running``, "✓" on complete, "!" on
 *   error.  Matches existing StatusPill but inline so we don't import
 *   the larger primitive twice on every card.
 * * Monospace input/output blocks; arbitrary JSON is pretty-printed.
 *
 * Accessibility
 * -------------
 * * Card root is ``role="status"`` so screen readers announce
 *   transitions (input-streaming → complete).
 * * Native ``<details>`` keeps keyboard navigation working without
 *   custom ARIA bookkeeping.
 */

import { type Component, Show } from "solid-js";

import type { ToolCallFrame } from "../../lib/tool-stream";
import { t } from "../../i18n";

export interface ToolCallCardProps {
  frame: ToolCallFrame;
  /** When provided, replaces the default inline output renderer. */
  renderOutput?: (output: unknown) => import("solid-js").JSX.Element;
}

const STATUS_ICON: Record<ToolCallFrame["status"], string> = {
  pending: "○",
  running: "◔",
  complete: "✓",
  error: "!",
};

/** Maps the canonical status to a translation key.  Resolved per
 *  render so a locale flip while a card is on screen updates the
 *  visible status word. */
const STATUS_KEY: Record<ToolCallFrame["status"], string> = {
  pending: "toolcall.status.pending",
  running: "toolcall.status.running",
  complete: "toolcall.status.complete",
  error:   "toolcall.status.error",
};

const STATUS_TONE: Record<ToolCallFrame["status"], string> = {
  pending: "text-text-tertiary",
  running: "text-text-secondary motion-safe:animate-pulse",
  complete: "text-text-primary",
  error: "text-status-error",
};

function prettyJson(v: unknown): string {
  if (v === undefined) return "";
  try {
    return typeof v === "string" ? v : JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

export const ToolCallCard: Component<ToolCallCardProps> = (props) => {
  return (
    <div
      role="group"
      aria-live="polite"
      aria-label={t("toolcall.label", { tool: props.frame.tool })}
      class="rounded-md border border-border-subtle bg-bg-elevated"
      style={{ "border-left": "2px solid var(--mode-accent)" }}
      data-amor-toolcall=""
      data-amor-tool={props.frame.tool}
      data-amor-status={props.frame.status}
    >
      <header class="flex items-center justify-between gap-2 px-3 py-1.5 text-xs">
        <div class="flex min-w-0 items-center gap-2">
          <span
            class={`inline-flex h-3.5 w-3.5 items-center justify-center text-[0.85rem] leading-none ${STATUS_TONE[props.frame.status]}`}
            aria-hidden="true"
          >
            {STATUS_ICON[props.frame.status]}
          </span>
          <code class="truncate font-mono text-[0.75rem] text-text-primary">
            {props.frame.tool}
          </code>
          <Show when={props.frame.meta?.iteration !== undefined}>
            <span class="text-[0.65rem] text-text-tertiary">
              {t("toolcall.iteration", { n: String(props.frame.meta?.iteration) })}
            </span>
          </Show>
        </div>
        <span
          class={`text-[0.65rem] uppercase tracking-wide ${STATUS_TONE[props.frame.status]}`}
        >
          {t(STATUS_KEY[props.frame.status])}
        </span>
      </header>

      <Show when={props.frame.input !== undefined || props.frame.inputDelta}>
        <details class="border-t border-border-subtle">
          <summary class="cursor-pointer px-3 py-1.5 text-[0.7rem] text-text-tertiary hover:text-text-secondary">
            {t("toolcall.input")}
          </summary>
          <pre class="max-h-40 overflow-auto px-3 pb-2 font-mono text-[0.7rem] text-text-secondary">
            {props.frame.input !== undefined
              ? prettyJson(props.frame.input)
              : props.frame.inputDelta}
          </pre>
        </details>
      </Show>

      <Show when={props.frame.output !== undefined}>
        <details class="border-t border-border-subtle" open>
          <summary class="cursor-pointer px-3 py-1.5 text-[0.7rem] text-text-tertiary hover:text-text-secondary">
            {t("toolcall.output")}
          </summary>
          <Show
            when={props.renderOutput}
            fallback={
              <pre
                class={[
                  "max-h-40 overflow-auto px-3 pb-2 font-mono text-[0.7rem]",
                  props.frame.isError
                    ? "text-status-error"
                    : "text-text-secondary",
                ].join(" ")}
              >
                {prettyJson(props.frame.output)}
              </pre>
            }
          >
            <div class="px-3 pb-2">
              {props.renderOutput?.(props.frame.output)}
            </div>
          </Show>
        </details>
      </Show>

      <Show when={props.frame.errorMessage}>
        <p class="border-t border-border-subtle px-3 py-1.5 text-[0.7rem] text-status-error">
          {props.frame.errorMessage}
        </p>
      </Show>
    </div>
  );
};
