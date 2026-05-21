import { type Component, type JSX, splitProps } from "solid-js";

export type Status = "healthy" | "warming" | "warning" | "failed";

export interface StatusPillProps extends JSX.HTMLAttributes<HTMLSpanElement> {
  status: Status;
  size?: "sm" | "md";
  /** Visible label.  When absent, the pill renders as a dot only and
   *  the ``aria-label`` becomes mandatory. */
  label?: string;
}

const STATUS_LABEL: Record<Status, string> = {
  healthy: "Healthy",
  warming: "Warming up",
  warning: "Warning",
  failed: "Failed",
};

const STATUS_COLOR: Record<Status, string> = {
  healthy: "var(--color-status-healthy)",
  warming: "var(--color-status-warming)",
  warning: "var(--color-status-warning)",
  failed: "var(--color-status-failed)",
};

/**
 * Status pill â€” the 4 states adopted across the diagnostics surface
 * (cf. design doc Â§5.2).  ``warming`` animates a pulse to signal
 * "loading"; the animation is disabled by ``prefers-reduced-motion``.
 *
 * Pass ``label=""`` to render the dot in icon-only mode (a tighter
 * card layout).  ``aria-label`` always falls back to the status
 * name so screen readers announce something meaningful even when
 * the visible text is suppressed.
 */
export const StatusPill: Component<StatusPillProps> = (props) => {
  const [local, rest] = splitProps(props, [
    "status",
    "size",
    "label",
    "class",
  ]);
  const size = () => local.size ?? "sm";
  /** Visible text â€” empty string OR undefined â†’ icon only. */
  const visibleText = (): string =>
    local.label && local.label.length > 0 ? local.label : "";
  /** ARIA label â€” always non-empty so SR users hear the status. */
  const a11yLabel = (): string => visibleText() || STATUS_LABEL[local.status];
  const iconOnly = () => visibleText() === "";

  return (
    <span
      class={[
        "inline-flex items-center rounded-full font-medium",
        "border border-border-subtle bg-bg-elevated text-text-display",
        iconOnly()
          ? size() === "sm"
            ? "h-4 w-4"
            : "h-5 w-5"
          : size() === "sm"
            ? "h-6 gap-1.5 px-2 text-xs"
            : "h-7 gap-1.5 px-3 text-sm",
        iconOnly() ? "justify-center" : "",
        local.class ?? "",
      ].join(" ")}
      role="status"
      aria-label={a11yLabel()}
      {...rest}
    >
      <span
        aria-hidden="true"
        class={[
          "h-2 w-2 rounded-full",
          local.status === "warming" ? "motion-safe:animate-pulse" : "",
        ].join(" ")}
        style={{ background: STATUS_COLOR[local.status] }}
      />
      {visibleText()}
    </span>
  );
};
