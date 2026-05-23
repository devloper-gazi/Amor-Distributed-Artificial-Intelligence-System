import { type Component, type JSX, splitProps, Show } from "solid-js";

export interface ProgressBarProps
  extends JSX.HTMLAttributes<HTMLDivElement> {
  /** 0..100, or ``null`` for indeterminate (animated stripe). */
  value: number | null;
  label?: string;
}

/**
 * Determinate / indeterminate progress bar.
 *
 * Determinate: explicit `value` 0..100 fills with the mode accent.
 * Indeterminate: `value=null` runs an animated stripe that respects
 * ``prefers-reduced-motion: reduce`` (becomes a static half-fill).
 */
export const ProgressBar: Component<ProgressBarProps> = (props) => {
  const [local, rest] = splitProps(props, ["value", "label", "class"]);
  const pct = () =>
    local.value === null
      ? null
      : Math.max(0, Math.min(100, local.value));

  return (
    <div
      class={["relative w-full", local.class ?? ""].join(" ")}
      role="progressbar"
      aria-valuemin="0"
      aria-valuemax="100"
      aria-valuenow={pct() ?? undefined}
      aria-label={local.label}
      {...rest}
    >
      <div class="h-1 w-full overflow-hidden rounded-full bg-bg-elevated-v25">
        <Show
          when={pct() !== null}
          fallback={
            <span
              class={[
                "block h-full w-1/3 rounded-full",
                "motion-safe:animate-[progress_1.4s_ease-in-out_infinite]",
                "motion-reduce:w-1/2",
              ].join(" ")}
              style={{ background: "var(--mode-accent)" }}
            />
          }
        >
          <span
            class="block h-full rounded-full transition-[width] duration-300"
            style={{
              width: `${pct()}%`,
              background: "var(--mode-accent)",
            }}
          />
        </Show>
      </div>
      {/* Inline keyframes for indeterminate stripe — small enough to
          live next to the component instead of leaking into theme.css. */}
      <style>{`
        @keyframes progress {
          0% { transform: translateX(-100%); }
          50% { transform: translateX(100%); }
          100% { transform: translateX(300%); }
        }
      `}</style>
    </div>
  );
};
