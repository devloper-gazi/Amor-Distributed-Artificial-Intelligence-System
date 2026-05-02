import { type Component, type JSX, splitProps } from "solid-js";

type Size = "sm" | "md";

export interface IconButtonProps
  extends Omit<JSX.ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  size?: Size;
  /** Required for screen readers — IconButtons have no visible label. */
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
        "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
        "focus-visible:outline-2 focus-visible:outline-offset-2",
        "transition-colors duration-100",
        "disabled:cursor-not-allowed disabled:opacity-50",
        SIZE_CLASS[size()],
        local.class ?? "",
      ].join(" ")}
      {...rest}
    >
      {local.children}
    </button>
  );
};
