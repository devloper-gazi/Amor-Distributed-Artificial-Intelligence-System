import { type Component } from "solid-js";

export interface SpinnerProps {
  size?: number;
  class?: string;
  /** Used for the polite live region announcement when the spinner
   *  represents a long-running operation. */
  label?: string;
}

/**
 * Spinner.  Animates a circle stroke under
 * ``@media (motion-safe)``; respects ``prefers-reduced-motion: reduce``
 * by switching to a static dot.
 */
export const Spinner: Component<SpinnerProps> = (props) => {
  const size = () => props.size ?? 16;
  return (
    <span
      role="status"
      aria-label={props.label ?? "Loading"}
      class={["inline-flex", props.class ?? ""].join(" ")}
      style={{ width: `${size()}px`, height: `${size()}px` }}
    >
      {/* motion-safe: real spin.  motion-reduce: static dot for
          users who set prefers-reduced-motion. */}
      <svg
        class="motion-safe:animate-spin motion-reduce:hidden"
        viewBox="0 0 24 24"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
      >
        <circle
          cx="12"
          cy="12"
          r="10"
          stroke="currentColor"
          stroke-opacity="0.2"
          stroke-width="3"
        />
        <path
          d="M22 12a10 10 0 0 1-10 10"
          stroke="currentColor"
          stroke-width="3"
          stroke-linecap="round"
        />
      </svg>
      <span
        class="motion-safe:hidden motion-reduce:inline-block rounded-full bg-current"
        style={{ width: `${size()}px`, height: `${size()}px` }}
        aria-hidden="true"
      />
    </span>
  );
};
