import { type Component, type JSX, splitProps } from "solid-js";

export interface DividerProps extends JSX.HTMLAttributes<HTMLHRElement> {
  orientation?: "horizontal" | "vertical";
}

/**
 * 1-pixel rule.  Vertical orientation requires the parent to be
 * a flex/grid container with a fixed height.
 */
export const Divider: Component<DividerProps> = (props) => {
  const [local, rest] = splitProps(props, ["orientation", "class"]);
  const isVertical = () => local.orientation === "vertical";
  return (
    <hr
      role="separator"
      aria-orientation={isVertical() ? "vertical" : "horizontal"}
      class={[
        "border-0 bg-border-subtle",
        isVertical() ? "h-full w-px" : "h-px w-full",
        local.class ?? "",
      ].join(" ")}
      {...rest}
    />
  );
};
