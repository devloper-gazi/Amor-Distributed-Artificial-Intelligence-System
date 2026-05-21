import {
  type Component,
  type JSX,
  createSignal,
  createUniqueId,
  Show,
} from "solid-js";

export interface TooltipProps {
  /** Content displayed inside the floating tooltip. */
  label: string;
  /** Anchored child â€” the trigger element. */
  children: JSX.Element;
  /** Where the tooltip appears relative to the anchor. */
  placement?: "top" | "bottom" | "left" | "right";
  /** ms before the tooltip opens on hover/focus.  Default 200. */
  openDelay?: number;
}

/**
 * Lightweight tooltip with no external positioning library.
 *
 * Hover delay (default 200 ms) prevents accidental flashes during
 * rapid pointer movement.  Keyboard focus opens immediately so users
 * tabbing through controls see the description without delay.
 *
 * For dynamic positioning (avoid viewport edges, etc.) we'll swap to
 * floating-ui in a later PR; for now CSS classes cover the 4 cardinal
 * placements which is sufficient for atom-level use.
 */
export const Tooltip: Component<TooltipProps> = (props) => {
  const [open, setOpen] = createSignal(false);
  const id = createUniqueId();
  let openTimer: ReturnType<typeof setTimeout> | undefined;

  const placement = () => props.placement ?? "top";
  const delay = () => props.openDelay ?? 200;

  const onPointerEnter = () => {
    clearTimeout(openTimer);
    openTimer = setTimeout(() => setOpen(true), delay());
  };
  const onPointerLeave = () => {
    clearTimeout(openTimer);
    setOpen(false);
  };
  const onFocus = () => setOpen(true);
  const onBlur = () => setOpen(false);

  const placementClass = () => {
    switch (placement()) {
      case "bottom":
        return "left-1/2 top-full mt-2 -translate-x-1/2";
      case "left":
        return "right-full top-1/2 mr-2 -translate-y-1/2";
      case "right":
        return "left-full top-1/2 ml-2 -translate-y-1/2";
      case "top":
      default:
        return "bottom-full left-1/2 mb-2 -translate-x-1/2";
    }
  };

  return (
    <span
      class="relative inline-flex"
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      onFocusIn={onFocus}
      onFocusOut={onBlur}
    >
      <span aria-describedby={open() ? id : undefined}>{props.children}</span>
      <Show when={open()}>
        <span
          id={id}
          role="tooltip"
          class={[
            "pointer-events-none absolute z-[var(--z-toast)]",
            "whitespace-nowrap rounded-sm border border-border-subtle",
            "bg-bg-elevated px-2 py-1 text-xs text-text-display shadow-md",
            placementClass(),
          ].join(" ")}
        >
          {props.label}
        </span>
      </Show>
    </span>
  );
};
