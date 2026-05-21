/**
 * Cycle UI v2.5 Phase 3 — sidebar search input + ⌘K kbd hint.
 *
 * Visual sketch (Research v2.5 §G.3):
 *   * Sits at the top of the SessionList sidebar
 *   * Search icon (◯) on the left
 *   * Single-line input — Tailwind v4 utilities only, no Kobalte
 *   * ⌘K kbd hint on the right — clicking opens the existing
 *     CommandPalette (Cycle B/C feature) via window event
 *   * Below md breakpoint the kbd hint hides (mobile users have
 *     no ⌘ key)
 *
 * The search itself is a thin wrapper around the existing
 * Sidebar Q query filter (already wired downstream); this component
 * exposes a controlled value + the platform-appropriate keyboard hint.
 *
 * Accessibility:
 *   * <label> wrapper provides an accessible name for the input
 *   * kbd uses <kbd> semantic tag
 *   * focus-within ring matches the rest of the chrome
 */

import { type Component, Show } from "solid-js";
import { t } from "../../i18n";

export interface SessionSearchProps {
  value: string;
  onInput: (next: string) => void;
  /** When provided, replaces the kbd-hint click behaviour.  Default:
   *  dispatches a "amor:open-palette" CustomEvent — the existing
   *  CommandPalette listens for this. */
  onOpenPalette?: () => void;
}

const isMacLike = (): boolean => {
  if (typeof navigator === "undefined") return false;
  return /Mac|iPod|iPhone|iPad/.test(navigator.platform);
};

export const SessionSearch: Component<SessionSearchProps> = (props) => {
  const modKey = isMacLike() ? "⌘" : "Ctrl";

  const openPalette = () => {
    if (props.onOpenPalette) {
      props.onOpenPalette();
      return;
    }
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("amor:open-palette"));
    }
  };

  return (
    <div class="px-3 pt-2 pb-1" data-amor-session-search="">
      <label
        class="flex items-center gap-2 rounded-md border border-border-subtle bg-bg-canvas px-2 py-1.5
               focus-within:ring-1 focus-within:ring-focus-ring focus-within:border-border-strong-v25
               transition-colors duration-150"
      >
        <span
          class="text-text-mute select-none"
          aria-hidden="true"
        >
          ◯
        </span>
        <input
          type="search"
          value={props.value}
          onInput={(e) => props.onInput(e.currentTarget.value)}
          placeholder={t("search.placeholder")}
          aria-label={t("search.placeholder")}
          class="flex-1 bg-transparent text-[13px] text-text-body outline-none
                 placeholder:text-text-mute"
          data-amor-session-search-input=""
        />
        <Show when={!props.value}>
          <button
            type="button"
            onClick={openPalette}
            class="hidden md:inline-flex amor-touch items-center gap-0.5 text-text-mute hover:text-text-body
                   focus-visible:outline-2 focus-visible:outline-offset-2"
            aria-label={t("search.open_palette_aria")}
            title={t("search.open_palette_aria")}
            data-amor-session-search-kbd=""
          >
            <kbd
              class="font-mono text-[10px] border border-border-subtle rounded px-1 py-0.5"
            >
              {modKey}
            </kbd>
            <kbd
              class="font-mono text-[10px] border border-border-subtle rounded px-1 py-0.5"
            >
              K
            </kbd>
          </button>
        </Show>
        <Show when={props.value}>
          <button
            type="button"
            onClick={() => props.onInput("")}
            class="amor-touch text-text-mute hover:text-text-body
                   focus-visible:outline-2 focus-visible:outline-offset-2"
            aria-label={t("search.clear_aria")}
            data-amor-session-search-clear=""
          >
            ×
          </button>
        </Show>
      </label>
    </div>
  );
};

export default SessionSearch;
