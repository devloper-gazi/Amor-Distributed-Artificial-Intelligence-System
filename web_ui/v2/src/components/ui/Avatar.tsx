import { type Component, type JSX, splitProps, Show } from "solid-js";

type Variant = "user" | "system" | "model";
type Size = 24 | 32;

export interface AvatarProps extends JSX.HTMLAttributes<HTMLSpanElement> {
  variant?: Variant;
  size?: Size;
  /** Two-letter initials fallback when no `src`. */
  initials?: string;
  src?: string;
}

const VARIANT_BG: Record<Variant, string> = {
  user: "bg-bg-elevated-v25 text-text-display",
  system: "bg-text-display text-text-inverse",
  model: "bg-bg-elevated-v25 text-text-body",
};

/**
 * Circular avatar.  Renders an image when `src` is given, otherwise
 * the first 2 chars of `initials` (uppercased).  Variant tints the
 * default background.
 */
export const Avatar: Component<AvatarProps> = (props) => {
  const [local, rest] = splitProps(props, [
    "variant",
    "size",
    "initials",
    "src",
    "class",
    "children",
  ]);
  const variant = () => local.variant ?? "user";
  const size = () => local.size ?? 32;
  return (
    <span
      class={[
        "inline-flex items-center justify-center overflow-hidden rounded-full",
        "font-medium",
        VARIANT_BG[variant()],
        local.class ?? "",
      ].join(" ")}
      style={{
        width: `${size()}px`,
        height: `${size()}px`,
        "font-size": size() === 24 ? "0.6rem" : "0.75rem",
      }}
      {...rest}
    >
      <Show
        when={local.src}
        fallback={(local.initials ?? "??").slice(0, 2).toUpperCase()}
      >
        <img
          src={local.src}
          alt=""
          class="h-full w-full object-cover"
          aria-hidden="true"
        />
      </Show>
    </span>
  );
};
