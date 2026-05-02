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
 * Status pill — the 4 states adopted across the diagnostics surface
 * (cf. design doc §5.2).  ``warming`` animates a pulse to signal
 * "loading"; the animation is disabled by ``prefers-reduced-motion``.
 */
export const StatusPill: Component<StatusPillProps> = (props) => {
  const [local, rest] = splitProps(props, [
    "status",
    "size",
    "label",
    "class",
  ]);
  const size = () => local.size ?? "sm";
  return (
    <span
      class={[
        "inline-flex items-center gap-1.5 rounded-full font-medium",
        "border border-border-subtle bg-bg-elevated text-text-primary",
        size() === "sm" ? "h-6 px-2 text-xs" : "h-7 px-3 text-sm",
        local.class ?? "",
      ].join(" ")}
      role="status"
      aria-label={local.label ?? STATUS_LABEL[local.status]}
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
      {local.label ?? STATUS_LABEL[local.status]}
    </span>
  );
};
