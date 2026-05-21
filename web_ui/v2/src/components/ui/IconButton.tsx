import { type Component, type JSX, splitProps } from "solid-js";

type Size = "sm" | "md";

export interface IconButtonProps
  extends Omit<JSX.ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  size?: Size;
  /** Required for screen readers Ã¢â‚¬â€ IconButtons have no visible label. */
  "aria-label": string;
  type?: "button" | "submit" | "reset";
}

const SIZE_CLASS: Record<Size, string> = {
  sm: "h-7 w-7",
  md: "h-8 w-8",
};

/**
 * Square icon-only button.  ``aria-label`` is mandatory at the type
 * level so contributors can't ship an unannouncible icon button.
 *
 * Cycle C Sprint 11 Day 4 Ã¢â‚¬â€ every IconButton picks up the
 * ``.amor-touch`` utility so on touch devices its hit-target is at
 * least 44Ãƒâ€”44 px (Apple HIG + WCAG 2.5.5).  Visual size stays
 * whatever ``SIZE_CLASS`` produced; the CSS bumps the *minimum*
 * dimensions only when ``@media (pointer: coarse)`` matches.
 */
export const IconButton: Component<IconButtonProps> = (props) => {
  const [local, rest] = splitProps(props, [
    "size",
    "type",
    "class",
    "children",
  ]);
  const size = () => local.size ?? "md";
  return (
    <button
      type={local.type ?? "button"}
      class={[
        "inline-flex items-center justify-center rounded-md",
        "text-text-body hover:bg-bg-hover hover:text-text-display",
        "focus-visible:outline-2 focus-visible:outline-offset-2",
        "transition-colors duration-100",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "amor-touch",
        SIZE_CLASS[size()],
        local.class ?? "",
      ].join(" ")}
      {...rest}
    >
      {local.children}
    </button>
  );
};
