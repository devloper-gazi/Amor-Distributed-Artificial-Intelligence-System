/**
 * Cycle UI v2.6.2 (D3) — inline SVG icon set.
 *
 * Hand-rolled stroke-based icons; no external dep.  Rationale:
 * Lucide-Solid would add ~8-10 KB gz for our ≤6 needed icons.
 * Inline SVG at 1.75 stroke-width (between Heroicons 1.5 and
 * Lucide 2.0) gives a premium weight that pairs well with the
 * 14-15 px label sizes used in the composer.
 *
 * Each icon:
 *   * viewBox 24×24 (Lucide standard so the tooling around it
 *     keeps the same coordinate intuition).
 *   * stroke="currentColor" — inherits text color via Tailwind.
 *   * strokeLinecap/strokeLinejoin "round" — soft endings, no
 *     boxy 8-bit feel.
 *   * No fill (transparent by default).
 *   * aria-hidden="true" — decorative, parent has aria-label.
 *
 * Sizing: parent wraps with h-? w-? Tailwind classes; we don't
 * hard-code a size here so callers can resize without props.
 */
import { type Component, type JSX } from "solid-js";

type IconProps = JSX.SvgSVGAttributes<SVGSVGElement>;

const baseProps = {
  width: "1em",
  height: "1em",
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  "stroke-width": "1.75",
  "stroke-linecap": "round",
  "stroke-linejoin": "round",
  "aria-hidden": "true",
} as const;

/** Send / submit — arrow pointing up.  Used in composer's
 *  primary action button after Cycle UI v2.6.2 (Karar D2). */
export const SendArrow: Component<IconProps> = (props) => (
  <svg {...baseProps} {...props}>
    <path d="M12 19V5" />
    <path d="m5 12 7-7 7 7" />
  </svg>
);

/** Attach / file picker — paperclip.  Replaces the text-only
 *  "Ekle" custom inline button (Karar D6). */
export const Paperclip: Component<IconProps> = (props) => (
  <svg {...baseProps} {...props}>
    <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 17.93 8.83l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
  </svg>
);

/** Close / dismiss — X mark.  Available for attachment chip
 *  remove buttons and modal dismissals. */
export const XMark: Component<IconProps> = (props) => (
  <svg {...baseProps} {...props}>
    <path d="M18 6 6 18" />
    <path d="m6 6 12 12" />
  </svg>
);

/** Chevron down — disclosure caret.  For mode picker, accordion
 *  sections, dropdown indicators.  Pair with `rotate-180` when
 *  the disclosure is expanded. */
export const ChevronDown: Component<IconProps> = (props) => (
  <svg {...baseProps} {...props}>
    <path d="m6 9 6 6 6-6" />
  </svg>
);
