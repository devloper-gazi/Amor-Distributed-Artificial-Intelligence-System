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
import { t } from "../../i18n";

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
      class="amor-enter mx-auto flex w-full max-w-2xl flex-col items-center
             justify-center px-4 py-16"
      data-amor-empty-state=""
    >
      <h1
        class="text-[28px] leading-8 font-semibold tracking-tight
               text-text-display mb-8 text-center"
      >
        {t("empty.greeting")}
      </h1>
      <div
        class="grid w-full grid-cols-1 gap-2 md:grid-cols-2"
        data-amor-empty-grid=""
      >
        <For each={SEEDS}>
          {(seed) => (
            <button
              type="button"
              onClick={() => props.onSeed(t(seed.promptKey), seed.mode)}
              class="group flex items-start gap-3 rounded-md border border-border-subtle
                     bg-bg-canvas p-3 text-left transition-colors duration-150
                     hover:border-border-strong-v25 hover:bg-bg-elevated
                     focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
              data-amor-empty-seed={seed.mode}
              aria-label={`${t(seed.labelKey)}: ${t(seed.promptKey)}`}
            >
              <span
                class="size-1.5 mt-2 rounded-full shrink-0"
                style={{
                  "background-color": `var(--color-mode-${seed.mode})`,
                }}
                aria-hidden="true"
              />
              <div class="min-w-0 flex-1">
                <div class="text-[10px] uppercase tracking-wider text-text-mute mb-0.5">
                  {t(seed.labelKey)}
                </div>
                <div class="text-[13px] leading-[19px] text-text-body">
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
