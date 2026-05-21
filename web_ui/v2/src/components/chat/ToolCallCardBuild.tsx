/**
 * Cycle UI v2.5 Phase 2 — Build-engine tool-call card.
 *
 * Renders one ``BuildToolCard`` payload (see ``lib/types.ts``) from
 * the Build pipeline's code_ready / test_ready / execution_result /
 * review_ready events.  Replaces the previous behaviour of appending
 * markdown fences inline into the assistant bubble.
 *
 * Visual conventions (see Research v2.5 §L):
 *   * Header: kind icon + truncated file path + status chip
 *   * Body: collapsible <details>; DiffBlock when language="diff",
 *     fenced markdown otherwise via renderMarkdown
 *   * Footer: status + duration_ms (tabular-nums) + extra meta keys
 *
 * The card sits inside MessageThread as a ``role: "tool"`` turn —
 * MessageThread routes the rendering (sibling to ApprovalPrompt
 * for approval turns).  No avatar / no role label header; the card
 * IS the chrome.
 *
 * Accessibility:
 *   * role="region" + aria-label so screen readers announce it as
 *     a discrete block.
 *   * Native <details>/<summary> for collapse — keyboard works
 *     out-of-box, no custom ARIA bookkeeping.
 */

import {
  type Component,
  Show,
  Suspense,
  createMemo,
  createSignal,
  lazy,
} from "solid-js";
import type { BuildToolCard } from "../../lib/types";
import { renderMarkdown } from "../../lib/sanitise";
import { t } from "../../i18n";

// DiffBlock is lazy-loaded — only Build payloads that emit a diff
// body (language=="diff") pull in the diff2html bundle.
const DiffBlock = lazy(
  () => import("./DiffBlock").then((m) => ({ default: m.DiffBlock })),
);

export interface ToolCallCardBuildProps {
  card: BuildToolCard;
}

/** Human-readable label per kind.  Falls back to the raw kind when
 *  the i18n key is missing. */
function kindLabelKey(kind: BuildToolCard["kind"]): string {
  switch (kind) {
    case "code_ready":         return "build.card.code";
    case "test_ready":         return "build.card.tests";
    case "execution_result":   return "build.card.execution";
    case "review_ready":       return "build.card.review";
  }
}

/** Tiny status-chip colour-mapper.  All values land in the global
 *  status palette (theme.css §status pills). */
const STATUS_TONE: Record<NonNullable<BuildToolCard["status"]>, string> = {
  ok:               "var(--color-status-healthy)",
  approved:         "var(--color-status-healthy)",
  pending:          "var(--color-status-warming)",
  failed:           "var(--color-status-failed)",
  needs_revision:   "var(--color-status-warming)",
};

const KIND_GLYPH: Record<BuildToolCard["kind"], string> = {
  code_ready:        "▲",
  test_ready:        "◇",
  execution_result:  "▶",
  review_ready:      "◐",
};

export const ToolCallCardBuild: Component<ToolCallCardBuildProps> = (props) => {
  const [open, setOpen] = createSignal(true);

  const hasBody = createMemo(() =>
    typeof props.card.body === "string" && props.card.body.length > 0,
  );

  const renderedBody = createMemo<string>(() => {
    const card = props.card;
    if (!card.body) return "";
    // Build's code_ready / test_ready come as already-fenced markdown
    // (the legacy reducer wrapped them); we strip the outer fence so
    // DiffBlock or our own renderer wraps cleanly.  Otherwise rely
    // on the renderer to fence it again from the language hint.
    return card.body;
  });

  // Light-weight inline status chip — saves importing StatusPill.
  const statusChip = () => {
    const s = props.card.status;
    if (!s) return null;
    return (
      <span
        class="inline-flex items-center gap-1 rounded-full px-1.5 py-px text-[0.6rem] font-medium uppercase tracking-wider"
        style={{
          color: STATUS_TONE[s],
          "background-color": "var(--color-bg-elevated, var(--bg-hover))",
        }}
        data-amor-build-status={s}
      >
        {t(`build.status.${s}`)}
      </span>
    );
  };

  return (
    <article
      role="region"
      aria-label={t(kindLabelKey(props.card.kind))}
      class="amor-enter mx-5 my-3 overflow-hidden rounded-md border border-border-subtle bg-bg-elevated"
      data-amor-build-card={props.card.kind}
    >
      {/* Header */}
      <header class="flex items-center gap-2 border-b border-border-subtle px-3 py-2">
        <span
          aria-hidden="true"
          class="flex h-3.5 w-3.5 items-center justify-center text-[0.85rem] leading-none"
          style={{ color: "var(--color-mode-build, var(--mode-accent))" }}
        >
          {KIND_GLYPH[props.card.kind]}
        </span>
        <span class="truncate text-[12px] font-medium text-text-display">
          {t(kindLabelKey(props.card.kind))}
        </span>
        <Show when={props.card.file}>
          {(file) => (
            <code class="ml-1 truncate font-mono text-[11px] text-text-subtle">
              {file()}
            </code>
          )}
        </Show>
        <div class="ml-auto flex items-center gap-2">
          {statusChip()}
          <Show when={hasBody()}>
            <button
              type="button"
              class="amor-touch flex h-6 w-6 items-center justify-center rounded text-text-subtle hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-offset-2"
              aria-expanded={open()}
              aria-label={open() ? t("build.card.collapse") : t("build.card.expand")}
              data-expanded={open() ? "1" : "0"}
              onClick={() => setOpen((o) => !o)}
            >
              <span class="amor-rotate-target leading-none" aria-hidden="true">
                ▾
              </span>
            </button>
          </Show>
        </div>
      </header>

      {/* Body */}
      <Show when={hasBody() && open()}>
        <div class="overflow-x-auto px-3 py-2 text-[12px] leading-5">
          <Show
            when={props.card.language === "diff"}
            fallback={
              <div
                class="prose-amor max-w-none font-mono"
                innerHTML={renderMarkdown(renderedBody())}
              />
            }
          >
            <Suspense
              fallback={
                <pre class="font-mono text-text-subtle">{renderedBody()}</pre>
              }
            >
              <DiffBlock
                diff={renderedBody()}
                filename={props.card.file}
                format="line-by-line"
              />
            </Suspense>
          </Show>
        </div>
      </Show>

      {/* Footer */}
      <Show when={props.card.durationMs != null || props.card.meta}>
        <footer class="flex items-center gap-3 border-t border-border-subtle px-3 py-1.5 text-[11px] text-text-subtle">
          <Show when={props.card.durationMs != null}>
            <span class="tabular-nums" data-amor-build-duration="">
              {props.card.durationMs}ms
            </span>
          </Show>
          <Show when={props.card.meta}>
            {(meta) => (
              <span class="truncate font-mono">
                {Object.entries(meta())
                  .slice(0, 3)
                  .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
                  .join(" · ")}
              </span>
            )}
          </Show>
        </footer>
      </Show>
    </article>
  );
};

export default ToolCallCardBuild;
