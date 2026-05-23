/**
 * Cycle UI v2.5 Phase 3 — empty-state seed prompt grid.
 *
 * Renders inside MessageThread when turns.length === 0.  Replaces
 * the static "No messages yet" fallback with four mode-themed
 * seed prompts the user can click to populate the composer.
 *
 * Visual sketch (Research v2.5 §H):
 *   * Centred display-sized greeting at the top
 *   * 4-row 1-col on mobile / 2x2 grid on md+
 *   * Each card: small mode-color dot + caption with mode label
 *     + body with the seed prompt text
 *   * No illustration, no keyboard banner — placeholder text
 *     already advertises ⌘+Enter
 *
 * Seed prompts come from i18n (empty.seed.*) so EN + TR stay
 * parallel out of the box.
 */

import { type Component, For } from "solid-js";
import type { ModeKey } from "../../lib/types";
import { t, localeUpper } from "../../i18n";
// Cycle UI v2.6 (Karar B) — time-of-day greeting replaces the static
// "empty.greeting" heading.  Greeting owns its own typography +
// `amor-enter` mount animation.
import { Greeting } from "./Greeting";

export interface EmptyStateProps {
  /** Called when the user clicks a seed card.  Composer's parent
   *  fills the textarea with the seed text + sets the suggested
   *  mode as the active override (no auto-submit). */
  onSeed: (text: string, mode: ModeKey) => void;
}

interface SeedDef {
  mode: ModeKey;
  /** i18n key under empty.seed.* */
  promptKey: string;
  /** i18n key for the human-readable mode label (mode.*.label) */
  labelKey: string;
}

const SEEDS: ReadonlyArray<SeedDef> = [
  {
    mode: "build",
    promptKey: "empty.seed.build",
    labelKey: "mode.build.label",
  },
  {
    mode: "research",
    promptKey: "empty.seed.research",
    labelKey: "mode.research.label",
  },
  {
    mode: "thinking",
    promptKey: "empty.seed.thinking",
    labelKey: "mode.thinking.label",
  },
  {
    mode: "quickcode",
    promptKey: "empty.seed.quickcode",
    labelKey: "mode.quickcode.label",
  },
];

export const EmptyState: Component<EmptyStateProps> = (props) => {
  return (
    <section
      class="mx-auto flex w-full max-w-2xl flex-col items-center
             justify-center px-4 py-16"
      data-amor-empty-state=""
    >
      {/* Cycle UI v2.6 — Greeting replaces the static empty.greeting
          heading.  Owns its own typography + amor-enter motion. */}
      <Greeting />
      {/* Cycle UI v2.6 (Karar C) — seed grid shrinks to a subdued
          row: borderless ghost buttons, smaller text, tighter
          spacing.  Hover lifts (border + bg).  Greeting carries the
          hero weight; grid becomes "did you mean…" suggestions. */}
      <div
        class="mt-10 grid w-full grid-cols-1 gap-1.5 md:grid-cols-2"
        data-amor-empty-grid=""
      >
        <For each={SEEDS}>
          {(seed) => (
            <button
              type="button"
              onClick={() => props.onSeed(t(seed.promptKey), seed.mode)}
              class="group flex items-start gap-2.5 rounded-md border border-transparent
                     bg-transparent p-2.5 text-left transition-colors duration-150
                     hover:border-border-subtle hover:bg-bg-elevated/60
                     focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
              data-amor-empty-seed={seed.mode}
              aria-label={`${t(seed.labelKey)}: ${t(seed.promptKey)}`}
            >
              <span
                class="size-1.5 mt-1.5 rounded-full shrink-0"
                style={{
                  "background-color": `var(--color-mode-${seed.mode})`,
                }}
                aria-hidden="true"
              />
              <div class="min-w-0 flex-1">
                <div class="text-[10px] tracking-wider text-text-mute mb-0.5">
                  {localeUpper(t(seed.labelKey))}
                </div>
                <div class="text-[12.5px] leading-[18px] text-text-subtle group-hover:text-text-body">
                  {t(seed.promptKey)}
                </div>
              </div>
            </button>
          )}
        </For>
      </div>
    </section>
  );
};

export default EmptyState;
