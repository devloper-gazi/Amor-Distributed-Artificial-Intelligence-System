/**
 * Cycle UI v2.5 Phase 2 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â slash-command suggestion overlay.
 *
 * Surfaces when the user types `/` at the start of the composer.
 * Renders a compact 6-chip popover (one per primary mode) plus a
 * keyboard hint.  Picking a chip OR typing a recognised alias from
 * `SLASH_ALIASES` resolves the mode; ESC closes; clicking outside
 * dismisses.
 *
 * Parser reuse: `parseSlashCommand` + `SLASH_ALIASES` from
 * `composer-parsers.ts`.  This component is presentation-only ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â it
 * doesn't own the active-mode state, just emits `onPick(mode)`.
 *
 * Accessibility: role="listbox" + arrow-key navigation +
 * aria-activedescendant.  44ÃƒÆ’Ã¢â‚¬â€44 touch targets via the .amor-touch
 * utility (Sprint 11).
 */

import {
  type Component,
  For,
  Show,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";

import type { ModeKey } from "../../lib/types";
import { SLASH_ALIASES, parseSlashCommand } from "./composer-parsers";
import { MODE_GLYPH } from "./composer-parsers";
import { modeLabel } from "../../i18n";
import { MODES, type ModeMeta } from "../../lib/types";
import { t } from "../../i18n";

interface SlashCommandOverlayProps {
  /** Current composer text.  When it stops starting with ``/`` the
   *  overlay closes itself. */
  text: string;
  /** Called when the user picks a mode chip OR presses Enter while
   *  the overlay is open.  Composer's submit handler is responsible
   *  for stripping the slash prefix afterwards. */
  onPick: (mode: ModeKey) => void;
  /** Called on ESC or click-outside. */
  onClose: () => void;
}

/** Build the chip list ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â one chip per primary mode (system mode is
 *  legacy + intentionally excluded from the composer's chip wall;
 *  the /system slash alias still resolves it for power users). */
const CHIP_MODES: ReadonlyArray<ModeMeta> = MODES.filter(
  (m) => m.key !== "system",
);

/** First slash alias that resolves to ``mode``; used as the chip's
 *  primary label.  E.g. build ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ /build, quickcode ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ /quick. */
function primaryAliasFor(mode: ModeKey): string {
  for (const [alias, target] of Object.entries(SLASH_ALIASES)) {
    if (target === mode) return alias;
  }
  return `/${mode}`;
}

export const SlashCommandOverlay: Component<SlashCommandOverlayProps> = (
  props,
) => {
  const [active, setActive] = createSignal(0);

  // Close when the user erases the slash (typed Backspace away).
  const stillRelevant = createMemo(() =>
    props.text.replace(/^\s+/, "").startsWith("/"),
  );

  // Live-parse to highlight the chip matching what the user has
  // typed so far (e.g. typing "/res" auto-highlights Research).
  const matched = createMemo<ModeKey | null>(() => {
    if (!stillRelevant()) return null;
    const { mode, slashUsed } = parseSlashCommand(props.text, "build");
    return slashUsed ? mode : null;
  });

  // Sync the active index with the auto-matched mode on every text
  // change so arrow-keys then pick up from the right place.
  const syncActive = () => {
    const m = matched();
    if (!m) return;
    const idx = CHIP_MODES.findIndex((c) => c.key === m);
    if (idx >= 0) setActive(idx);
  };

  // Keyboard navigation ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â composer's textarea owns key events; this
  // overlay listens at document level only for ESC.
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      props.onClose();
      return;
    }
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(CHIP_MODES.length - 1, i + 1));
      return;
    }
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
      return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      // Composer's onSubmit handler already commits on plain Enter
      // when the slash maps to a valid mode; this branch is only for
      // the special case where the user has typed `/x` (no full
      // alias yet) and presses Tab to autocomplete the highlighted
      // chip.
      if (e.key === "Tab") {
        const target = CHIP_MODES[active()];
        if (target) {
          e.preventDefault();
          props.onPick(target.key);
        }
      }
    }
  };

  onMount(() => {
    document.addEventListener("keydown", onKey);
    syncActive();
  });
  onCleanup(() => {
    document.removeEventListener("keydown", onKey);
  });

  return (
    <Show when={stillRelevant()}>
      <div
        role="listbox"
        aria-label={t("composer.slash_overlay_aria")}
        aria-activedescendant={`amor-slash-chip-${active()}`}
        class="absolute bottom-[100%] left-0 z-[var(--z-dropdown)] mb-2
               w-[min(360px,100%)] rounded-md border border-border-subtle
               bg-bg-elevated p-1 shadow-md grid grid-cols-2 gap-1"
        data-amor-slash-overlay=""
      >
        <For each={CHIP_MODES}>
          {(meta, i) => {
            const isActive = () => i() === active();
            const isMatched = () => matched() === meta.key;
            return (
              <button
                type="button"
                role="option"
                id={`amor-slash-chip-${i()}`}
                aria-selected={isActive()}
                onMouseDown={(e: MouseEvent) => {
                  // mousedown (not click) keeps textarea focus.
                  e.preventDefault();
                  props.onPick(meta.key);
                }}
                onMouseMove={() => setActive(i())}
                class={[
                  "amor-touch flex items-center gap-2 rounded px-2 py-1.5",
                  "text-left text-[13px] transition-colors duration-100",
                  isActive()
                    ? "bg-bg-hover text-text-display"
                    : "text-text-body hover:bg-bg-hover hover:text-text-display",
                  isMatched() ? "ring-1 ring-focus-ring" : "",
                ].join(" ")}
                data-amor-slash-mode={meta.key}
                data-amor-slash-matched={isMatched() ? "1" : "0"}
              >
                <span
                  class="flex h-3.5 w-3.5 items-center justify-center text-[0.85rem] leading-none"
                  style={{
                    color: `var(--color-mode-${meta.key}, var(--text-secondary))`,
                  }}
                  aria-hidden="true"
                >
                  {MODE_GLYPH[meta.key]}
                </span>
                <span class="flex min-w-0 flex-col">
                  <span class="truncate font-medium">{modeLabel(meta)}</span>
                  <span class="truncate text-[0.7rem] text-text-subtle">
                    {primaryAliasFor(meta.key)}
                  </span>
                </span>
              </button>
            );
          }}
        </For>
      </div>
    </Show>
  );
};

export default SlashCommandOverlay;
