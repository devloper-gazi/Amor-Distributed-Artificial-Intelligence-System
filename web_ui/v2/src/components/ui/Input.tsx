import { type Component, type JSX, splitProps, Show } from "solid-js";

export interface InputProps
  extends Omit<
    JSX.InputHTMLAttributes<HTMLInputElement>,
    "prefix"
  > {
  invalid?: boolean;
  /** Renders a small node before the input (e.g. an icon glyph).
   *  ``prefix`` shadows the reserved RDFa attribute via Omit above. */
  prefix?: JSX.Element;
  /** Renders a small node after the input (e.g. clear button). */
  suffix?: JSX.Element;
}

/**
 * Text-style input with optional prefix/suffix slots.
 *
 * The `invalid` flag flips the border to the failed-status colour and
 * sets `aria-invalid` so assistive tech announces the state.
 */
export const Input: Component<InputProps> = (props) => {
  const [local, rest] = splitProps(props, [
    "class",
    "invalid",
    "prefix",
    "suffix",
    "disabled",
  ]);
  return (
    <div
      class={[
        "flex h-10 items-center gap-2 rounded-md border",
        "bg-bg-elevated px-3 transition-colors",
        local.invalid
          ? "border-status-failed"
          : "border-border-default focus-within:border-border-strong",
        local.disabled ? "opacity-50" : "",
        local.class ?? "",
      ].join(" ")}
    >
      <Show when={local.prefix}>
        <span class="flex items-center text-text-tertiary" aria-hidden="true">
          {local.prefix}
        </span>
      </Show>
      <input
        class={[
          "min-w-0 flex-1 bg-transparent text-sm",
          "text-text-primary placeholder:text-text-tertiary",
          "outline-none disabled:cursor-not-allowed",
        ].join(" ")}
        disabled={local.disabled}
        aria-invalid={local.invalid ? "true" : undefined}
        {...rest}
      />
      <Show when={local.suffix}>
        <span class="flex items-center text-text-tertiary">{local.suffix}</span>
      </Show>
    </div>
  );
};
