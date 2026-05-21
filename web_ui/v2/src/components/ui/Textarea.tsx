import { type Component, type JSX, splitProps, onMount } from "solid-js";

export interface TextareaProps
  extends JSX.TextareaHTMLAttributes<HTMLTextAreaElement> {
  /** Min visible rows.  The autoresize JS fallback uses this as the
   *  floor when the textarea is empty. */
  minRows?: number;
  /** Max visible rows before scroll.  Caps growth so a long paste
   *  doesn't push the composer off-screen. */
  maxRows?: number;
}

/**
 * Autoresizing textarea.
 *
 * Uses the modern `field-sizing: content` CSS rule (Chrome 123+,
 * Safari 17.4+) when supported, falling back to a JS resize handler
 * for Firefox + older browsers.  The CSS path runs zero JS work.
 */
export const Textarea: Component<TextareaProps> = (props) => {
  const [local, rest] = splitProps(props, [
    "class",
    "minRows",
    "maxRows",
    "rows",
    "ref",
    "onInput",
  ]);

  let textareaRef: HTMLTextAreaElement | undefined;
  // ``rows`` from JSX.TextareaHTMLAttributes is typed as
  // ``number | string`` because HTML accepts both ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â coerce so the
  // arithmetic below stays numeric.
  const min = (): number =>
    local.minRows ?? Number(local.rows) ?? 1;
  const max = (): number => local.maxRows ?? 12;

  /** JS fallback for browsers that don't support field-sizing.  Reads
   *  scrollHeight, clamps between min/max line heights, applies as
   *  inline style.  Only fires when CSS.supports() returns false. */
  const autoresize = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    const lineH = parseFloat(getComputedStyle(el).lineHeight) || 20;
    const padY =
      parseFloat(getComputedStyle(el).paddingTop) +
      parseFloat(getComputedStyle(el).paddingBottom);
    const minPx = min() * lineH + padY;
    const maxPx = max() * lineH + padY;
    el.style.height = `${Math.min(maxPx, Math.max(minPx, el.scrollHeight))}px`;
  };

  const supportsFieldSizing = () =>
    typeof CSS !== "undefined" &&
    typeof CSS.supports === "function" &&
    CSS.supports("field-sizing", "content");

  onMount(() => {
    if (!textareaRef) return;
    if (!supportsFieldSizing()) {
      autoresize(textareaRef);
    }
  });

  return (
    <textarea
      ref={(el) => {
        textareaRef = el;
        if (typeof local.ref === "function") {
          (local.ref as (e: HTMLTextAreaElement) => void)(el);
        }
      }}
      rows={min()}
      onInput={(e) => {
        if (!supportsFieldSizing()) {
          autoresize(e.currentTarget);
        }
        if (typeof local.onInput === "function") {
          (local.onInput as JSX.EventHandler<HTMLTextAreaElement, InputEvent>)(
            e,
          );
        }
      }}
      class={[
        "w-full resize-none rounded-md border border-border-strong-v25",
        "bg-bg-elevated px-3 py-2 text-sm text-text-display",
        "placeholder:text-text-subtle",
        "outline-none focus:border-border-strong",
        // Modern path ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â zero JS resize work where supported.
        "[field-sizing:content]",
        local.class ?? "",
      ].join(" ")}
      {...rest}
    />
  );
};
