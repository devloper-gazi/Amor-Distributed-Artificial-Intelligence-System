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

const VARIANT_CLASS: Record<Variant, string> = {
  primary:
    "bg-text-primary text-text-inverse hover:opacity-90 active:opacity-80 disabled:opacity-50",
  secondary:
    "border border-border-default bg-bg-elevated text-text-primary hover:bg-bg-hover disabled:opacity-50",
  ghost:
    "text-text-primary hover:bg-bg-hover disabled:opacity-50",
  danger:
    "border border-status-failed/40 text-status-failed hover:bg-status-failed/10 disabled:opacity-50",
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
        "font-medium transition-[background-color,opacity] duration-100",
        "focus-visible:outline-2 focus-visible:outline-offset-2",
        "disabled:cursor-not-allowed",
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
