/**
 * Cycle C Sprint 11 Day 1 — viewport / breakpoint hook.
 *
 * Tracks the three signals every responsive Solid component needs:
 *
 * * ``width`` / ``height``        — layout viewport size (px)
 * * ``breakpoint``                 — resolved Tailwind-style label
 *                                    ("xs" | "sm" | "md" | "lg" | "xl")
 * * ``isMobile``                   — convenience: ``width < 768``
 * * ``keyboardOffset``             — ``visualViewport`` delta in px
 *                                    (>0 when an on-screen keyboard
 *                                    is covering the bottom of the
 *                                    layout viewport).  iOS Safari +
 *                                    Android Chrome both emit
 *                                    ``visualViewport.resize`` when
 *                                    the keyboard opens.
 *
 * Pure-Solid signals — no React.useEffect / no global event-listener
 * leaks.  ``onCleanup`` removes every listener when the consuming
 * component unmounts.
 *
 * Why a hook instead of a CSS-only solution: the bottom-sheet
 * composer (Sprint 11 Day 3) needs to *push itself up* by exactly
 * ``keyboardOffset`` px, not by ``vh`` units — the latter doesn't
 * shrink when the keyboard appears in iOS Safari.  ``visualViewport``
 * is the canonical workaround.
 */

import { createSignal, onCleanup, onMount } from "solid-js";


export type Breakpoint = "xs" | "sm" | "md" | "lg" | "xl";

/** Tailwind v4-aligned breakpoints (px).  Keep these in sync with
 *  ``tailwind.config.*`` if/when we customise — today the defaults
 *  apply (sm:640, md:768, lg:1024, xl:1280). */
export const BREAKPOINTS: Readonly<Record<Breakpoint, number>> = {
  xs: 0,
  sm: 640,
  md: 768,
  lg: 1024,
  xl: 1280,
};

/** ``isMobile`` boundary (Cycle C plan: <768 px = mobile shell). */
export const MOBILE_BREAKPOINT_PX = BREAKPOINTS.md;


export function classifyWidth(width: number): Breakpoint {
  if (width >= BREAKPOINTS.xl) return "xl";
  if (width >= BREAKPOINTS.lg) return "lg";
  if (width >= BREAKPOINTS.md) return "md";
  if (width >= BREAKPOINTS.sm) return "sm";
  return "xs";
}


export interface ViewportSnapshot {
  width: number;
  height: number;
  breakpoint: Breakpoint;
  isMobile: boolean;
  keyboardOffset: number;
}


function readSnapshot(): ViewportSnapshot {
  if (typeof window === "undefined") {
    // SSR / test fallback — pick a desktop default.
    return {
      width: BREAKPOINTS.lg,
      height: 800,
      breakpoint: "lg",
      isMobile: false,
      keyboardOffset: 0,
    };
  }
  const width = window.innerWidth;
  const height = window.innerHeight;
  const vv = window.visualViewport;
  // Keyboard offset = layout-viewport height - visual-viewport height
  // Clamped at zero so a notch / browser chrome change doesn't
  // produce a negative number.
  const keyboardOffset = vv
    ? Math.max(0, Math.round(height - vv.height - vv.offsetTop))
    : 0;
  return {
    width,
    height,
    breakpoint: classifyWidth(width),
    isMobile: width < MOBILE_BREAKPOINT_PX,
    keyboardOffset,
  };
}


/**
 * Returns a Solid accessor returning the current viewport snapshot.
 * Use it inside any component:
 *
 *     const viewport = useViewport();
 *     <Show when={viewport().isMobile}>
 *       <MobileShell />
 *     </Show>
 */
export function useViewport() {
  const [snap, setSnap] = createSignal<ViewportSnapshot>(readSnapshot());

  const update = () => setSnap(readSnapshot());

  onMount(() => {
    if (typeof window === "undefined") return;
    update();
    window.addEventListener("resize", update);
    window.addEventListener("orientationchange", update);
    const vv = window.visualViewport;
    if (vv) {
      vv.addEventListener("resize", update);
      vv.addEventListener("scroll", update);
    }
  });

  onCleanup(() => {
    if (typeof window === "undefined") return;
    window.removeEventListener("resize", update);
    window.removeEventListener("orientationchange", update);
    const vv = window.visualViewport;
    if (vv) {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    }
  });

  return snap;
}


// ─── test hook ──────────────────────────────────────────────────


/** Read a single snapshot without subscribing.  Useful for tests
 *  + for one-off non-reactive lookups. */
export function viewportSnapshot(): ViewportSnapshot {
  return readSnapshot();
}
