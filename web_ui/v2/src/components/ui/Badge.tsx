import { type Component, type JSX, splitProps } from "solid-js";

export interface BadgeProps extends JSX.HTMLAttributes<HTMLSpanElement> {
  variant?: "neutral" | "accent";
  size?: "sm" | "md";
}

/**
 * Compact label / count badge.  Mode accent variant pulls from
 * ``--mode-accent`` via the ``[data-mode]`` ancestor.
 */
export const Badge: Component<BadgeProps> = (props) => {
  const [local, rest] = splitProps(props, [
    "variant",
    "size",
    "class",
    "children",
  ]);
  const variant = () => local.variant ?? "neutral";
  const size = () => local.size ?? "sm";
  return (
    <span
      class={[
        "inline-flex items-center justify-center rounded-full font-medium",
        size() === "sm" ? "h-5 px-2 text-xs" : "h-6 px-2.5 text-sm",
        variant() === "accent"
          ? "text-text-inverse"
          : "border border-border-subtle bg-bg-tertiary text-text-secondary",
        local.class ?? "",
      ].join(" ")}
      style={
        variant() === "accent"
          ? { background: "var(--mode-accent)" }
          : undefined
      }
      {...rest}
    >
      {local.children}
    </span>
  );
};
