/**
 * Cycle C Sprint 11 Day 3 — keyboard-aware bottom-sheet wrapper.
 *
 * Wraps a child element (typically the UnifiedComposer or a per-mode
 * composer) and:
 *
 * * On desktop / tablet (``viewport.isMobile === false``) renders
 *   children as-is — zero overhead, zero layout shift.
 * * On mobile, fixes the wrapper to the bottom of the screen with
 *   safe-area-bottom padding (so content doesn't sit under the iOS
 *   home indicator) and translates it up by ``keyboardOffset`` px
 *   when the on-screen keyboard appears.  ``visualViewport.height``
 *   shrinks reliably across iOS Safari + Android Chrome; this is
 *   the canonical workaround for the "fixed bottom moves with the
 *   keyboard" mobile-web bug.
 *
 * The element's normal layout flow gets a sibling spacer matching
 * the wrapper height (read via ``ResizeObserver``) so scroll content
 * doesn't disappear behind the sheet on mobile.
 *
 * Why a wrapper rather than baking it into UnifiedComposer: the
 * Build / Research / Thinking routes each have their own composer
 * wrapper for streaming-error states; one shared wrapper means
 * every composer benefits without duplicating the keyboard-aware
 * code.
 */

import {
  type Component,
  type JSX,
  Show,
  createEffect,
  createSignal,
  onCleanup,
} from "solid-js";

import { useViewport } from "../../lib/viewport";


export interface BottomSheetProps {
  children: JSX.Element;
}


export const BottomSheet: Component<BottomSheetProps> = (props) => {
  const viewport = useViewport();
  const [sheetHeight, setSheetHeight] = createSignal(0);
  let sheetRef: HTMLDivElement | undefined;

  // Track our own height so the spacer reserves the right gap above
  // it.  ResizeObserver fires when the textarea grows / shrinks —
  // exactly the trigger we want.
  createEffect(() => {
    if (!sheetRef || !viewport().isMobile) {
      setSheetHeight(0);
      return;
    }
    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      if (!e) return;
      setSheetHeight(Math.round(e.contentRect.height));
    });
    ro.observe(sheetRef);
    onCleanup(() => ro.disconnect());
  });

  const offset = () => (viewport().isMobile ? viewport().keyboardOffset : 0);

  return (
    <Show
      when={viewport().isMobile}
      fallback={<>{props.children}</>}
    >
      {/* Spacer — reserves vertical room above the fixed sheet so
          scrollable content doesn't end up under it. */}
      <div
        aria-hidden="true"
        data-amor-bottom-sheet-spacer=""
        style={{ height: `${sheetHeight()}px` }}
      />
      {/* Fixed bottom sheet — translates up when the keyboard
          appears.  Tailwind handles the visual padding; the safe-
          area utility adds the device-specific bottom inset. */}
      <div
        ref={(el) => (sheetRef = el)}
        class="fixed inset-x-0 bottom-0 z-[var(--z-overlay)] amor-safe-bottom amor-safe-x"
        style={{
          // ``translateY`` keeps GPU-compositor friendliness; offset
          // is dynamic per visualViewport.resize event.
          transform: offset() > 0 ? `translateY(-${offset()}px)` : undefined,
          // Match the page's elevated background so the sheet reads
          // as a continuation of the layout, not a floating panel.
          background: "var(--color-bg-secondary)",
          "border-top": "1px solid var(--color-border-subtle)",
        }}
        data-amor-bottom-sheet=""
        data-amor-keyboard-offset={offset()}
      >
        {props.children}
      </div>
    </Show>
  );
};
