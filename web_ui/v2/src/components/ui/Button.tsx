import { type Component, type JSX, splitProps, Show } from "solid-js";
import { Spinner } from "./Spinner";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

export interface ButtonProps
  extends Omit<JSX.ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  /** Override the default `type="button"` (submit/reset). */
  type?: "button" | "submit" | "reset";
}

// Cycle UI v2.6.2 (D1) — `active:scale-[0.98]` adds Linear/Vercel-style
// press feedback to every variant.  `transform-gpu` promotes the
// element to its own compositor layer so the scale animation is
// jank-free.  prefers-reduced-motion (motion.css:181-210) zeros the
// transform automatically — no extra guard needed here.
const VARIANT_CLASS: Record<Variant, string> = {
  primary:
    "bg-text-display text-text-inverse hover:opacity-90 active:scale-[0.98] disabled:opacity-50 transform-gpu",
  secondary:
    "border border-border-strong-v25 bg-bg-elevated text-text-display hover:bg-bg-hover active:scale-[0.98] disabled:opacity-50 transform-gpu",
  ghost:
    "text-text-display hover:bg-bg-hover active:scale-[0.98] disabled:opacity-50 transform-gpu",
  danger:
    "border border-status-failed/40 text-status-failed hover:bg-status-failed/10 active:scale-[0.98] disabled:opacity-50 transform-gpu",
};

const SIZE_CLASS: Record<Size, string> = {
  sm: "h-8 px-3 text-sm rounded-md",
  md: "h-10 px-4 text-sm rounded-md",
};

/**
 * Button atom.
 *
 * Variants: primary / secondary / ghost / danger.
 * Sizes: sm (h-8) / md (h-10).
 * `loading=true` swaps children for a Spinner and keeps the button
 * disabled.  `type` defaults to "button" so the atom is safe inside a
 * `<form>` without accidentally submitting.
 */
export const Button: Component<ButtonProps> = (props) => {
  const [local, rest] = splitProps(props, [
    "variant",
    "size",
    "loading",
    "disabled",
    "type",
    "class",
    "children",
  ]);
  const variant = () => local.variant ?? "primary";
  const size = () => local.size ?? "md";
  return (
    <button
      type={local.type ?? "button"}
      disabled={local.disabled || local.loading}
      class={[
        "inline-flex items-center justify-center gap-2",
        // Cycle UI v2.6.2 (D1) — extend transition to cover the new
        // `active:scale-[0.98]` press feedback alongside background +
        // opacity.  120ms (was 100ms) lands somewhere between Linear
        // and Vercel's button motion — fast enough to feel responsive,
        // slow enough to register.
        "font-medium transition-[background-color,opacity,transform] duration-150",
        "focus-visible:outline-2 focus-visible:outline-offset-2",
        "disabled:cursor-not-allowed",
        // Sprint 11 Day 4 — guarantee 44×44 hit area on coarse-pointer
        // devices.  Visual size stays whatever ``SIZE_CLASS`` set; the
        // utility only enlarges when @media (pointer: coarse) hits.
        "amor-touch",
        SIZE_CLASS[size()],
        VARIANT_CLASS[variant()],
        local.class ?? "",
      ].join(" ")}
      {...rest}
    >
      <Show when={local.loading}>
        <Spinner size={size() === "sm" ? 14 : 16} />
      </Show>
      {local.children}
    </button>
  );
};
