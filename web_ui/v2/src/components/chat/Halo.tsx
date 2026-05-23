/**
 * Cycle UI v2.6 (Karar A + H) — Halo backdrop.
 *
 * Atmospheric radial gradient painted onto the chat canvas as a
 * decorative backdrop.  Mode-tinted: the active mode tints the halo
 * via OKLch `--color-halo-tint-{mode}` token; home/no-mode state
 * falls back to the neutral `--color-halo-base`.
 *
 * Layout: `position: fixed; inset: 0; z-index: -1` so it sits behind
 * everything in the chat canvas without participating in layout.
 * `pointer-events: none` ensures no interaction interception.
 * `aria-hidden="true"` because the halo is purely decorative.
 *
 * Motion language:
 *   * Default — composer not focused: opacity = `--color-halo-alpha-base`
 *     (0.55), `amor-blob` drift loops at 14s (compositor-only via
 *     `transform: translate3d(...) scale(...)`).
 *   * Composer focus — when `focused` prop flips true: opacity ramps
 *     to `--color-halo-alpha-focus` (0.85) over 240ms easeOut + scale
 *     1.0 → 1.02; the radial position stays centered.
 *   * `prefers-reduced-motion: reduce` — kill the blob keyframe; static
 *     gradient only.  Focus opacity still ramps (opacity-only motion
 *     is allowed under reduced-motion per WCAG 2.3.3 AAA which we
 *     opt-into anyway).
 *
 * Performance:
 *   * Single GPU layer (`will-change: transform, opacity`).
 *   * `contain: paint` so the halo's repaint stays inside its own
 *     box even when child layouts change.
 *   * Inline `--halo-tint` CSS var binding so Solid's reactivity flips
 *     the tint via a single style write per mode change — no class
 *     toggle, no DOM swap.
 *
 * Mount: rendered inside `routes/UnifiedChat.tsx` as the first child
 * of the outer div, before TopBar.  Sits at z-index 0 of that div,
 * but its own `position: fixed` lifts it to the viewport.
 */

import { type Component, createMemo } from "solid-js";
import { haloTint } from "../../lib/mode-color";
import type { ModeKey } from "../../lib/types";

export interface HaloProps {
  /** Active mode (from classifier or user override); `null` → neutral
   *  base tint, no mode dominance.  Pre-classifier home state. */
  mode: ModeKey | null | undefined;
  /** Composer focused?  Drives the opacity + scale ramp.  Default
   *  `false` keeps the halo at its rest state. */
  focused?: boolean;
}

export const Halo: Component<HaloProps> = (props) => {
  const tint = createMemo(() => haloTint(props.mode ?? undefined));

  return (
    <div
      aria-hidden="true"
      class="amor-halo"
      style={{
        "--halo-tint": tint(),
        "--halo-alpha": props.focused
          ? "var(--color-halo-alpha-focus)"
          : "var(--color-halo-alpha-base)",
        "--halo-scale": props.focused ? "1.02" : "1",
      }}
    />
  );
};

export default Halo;
