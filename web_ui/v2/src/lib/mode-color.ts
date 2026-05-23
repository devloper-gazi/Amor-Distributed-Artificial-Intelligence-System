/**
 * Cycle UI v2.6 (Karar A + L) — mode color helpers.
 *
 * Tek source-of-truth for mapping a mode key to its OKLch CSS var.  Both
 * the SessionList chip-color path (was previously embedded in
 * `components/shell/SessionList.tsx:102-112`) and the new Halo backdrop
 * (`components/chat/Halo.tsx`) consume this helper, so changing the
 * vocabulary in one place propagates everywhere.
 *
 * Why `var(--color-mode-…)` strings instead of the resolved OKLch tuple?
 * Tailwind v4 + the `@theme` block already publish the variables; we
 * want components to inherit the live light/dark/system value rather
 * than capture a snapshot at render time.  Inline styles read
 * `var(--color-mode-build)` and the browser does the resolution.
 *
 * The fallback path (`var(--color-text-mute)`) covers unknown modes —
 * activity rows that arrived from a legacy session before mode
 * inference, or test fixtures.  Mute, not bright, so the chip
 * de-emphasises itself.
 */

/** Canonical mode keys accepted across the surface.  Mirrors
 *  ``ModeKey`` in `lib/types.ts` plus the legacy ``"code"`` alias that
 *  earlier sessions persisted before Cycle UI v2.5 renamed it to
 *  ``"build"``. */
export type ModeColorInput =
  | "research"
  | "thinking"
  | "build"
  | "code"
  | "consortium"
  | "sentinel"
  | "system"
  | "quickcode"
  | string
  | undefined;

/**
 * Return the CSS `var(--color-mode-…)` reference for a mode.  Inline
 * style consumers do `style={{ background: modeColorVar(mode) }}` and
 * inherit live theme switching.
 *
 * Legacy "code" alias maps to "build" — the v2.5 rename kept legacy
 * sessions readable without a backfill.
 */
export function modeColorVar(mode: ModeColorInput): string {
  switch (mode) {
    case "research":   return "var(--color-mode-research)";
    case "thinking":   return "var(--color-mode-thinking)";
    case "build":
    case "code":       return "var(--color-mode-build)";
    case "consortium": return "var(--color-mode-consortium)";
    case "sentinel":   return "var(--color-mode-sentinel)";
    case "system":     return "var(--color-mode-system)";
    case "quickcode":  return "var(--color-mode-quickcode)";
    default:           return "var(--color-text-mute)";
  }
}

/**
 * Return the CSS `var(--color-halo-tint-…)` reference for a mode.
 * Cycle UI v2.6 Halo backdrop reads this; falls back to the neutral
 * `--color-halo-base` when the mode isn't recognised (or when home
 * has no active mode yet — pre-classifier state).
 *
 * `confidence` parameter is reserved for v2.7's confidence-modulated
 * tint blending (currently ignored — the v2.6 Halo only swaps tint
 * on commit, not on confidence ramp).  Accepting it now keeps the
 * call-site API stable across the v2.6 → v2.7 cycle.
 */
export function haloTint(
  mode: ModeColorInput,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  _confidence?: number,
): string {
  switch (mode) {
    case "research":   return "var(--color-halo-tint-research)";
    case "thinking":   return "var(--color-halo-tint-thinking)";
    case "build":
    case "code":       return "var(--color-halo-tint-build)";
    case "consortium": return "var(--color-halo-tint-consortium)";
    case "sentinel":   return "var(--color-halo-tint-sentinel)";
    case "system":     return "var(--color-halo-tint-system)";
    case "quickcode":  return "var(--color-halo-tint-quickcode)";
    default:           return "var(--color-halo-base)";
  }
}
