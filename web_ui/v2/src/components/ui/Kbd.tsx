import { type Component, type JSX, splitProps } from "solid-js";

export interface KbdProps extends JSX.HTMLAttributes<HTMLElement> {
  /** Auto-substitutes ``Mod`` for the platform-correct meta key glyph
   *  (``⌘`` on macOS, ``Ctrl`` everywhere else). */
  children: string;
}

const isMac = () =>
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/.test(navigator.platform);

const SUBSTITUTIONS_MAC: Array<[RegExp, string]> = [
  [/\bMod\b/g, "⌘"],
  [/\bCmd\b/g, "⌘"],
  [/\bShift\b/g, "⇧"],
  [/\bAlt\b/g, "⌥"],
  [/\bCtrl\b/g, "⌃"],
  [/\bEnter\b/g, "↵"],
  [/\bEsc\b/g, "Esc"],
  [/\bArrowUp\b/g, "↑"],
  [/\bArrowDown\b/g, "↓"],
  [/\bArrowLeft\b/g, "←"],
  [/\bArrowRight\b/g, "→"],
];

const SUBSTITUTIONS_OTHER: Array<[RegExp, string]> = [
  [/\bMod\b/g, "Ctrl"],
  [/\bShift\b/g, "Shift"],
  [/\bAlt\b/g, "Alt"],
  [/\bEnter\b/g, "↵"],
  [/\bEsc\b/g, "Esc"],
  [/\bArrowUp\b/g, "↑"],
  [/\bArrowDown\b/g, "↓"],
  [/\bArrowLeft\b/g, "←"],
  [/\bArrowRight\b/g, "→"],
];

const formatGlyphs = (s: string): string => {
  const subs = isMac() ? SUBSTITUTIONS_MAC : SUBSTITUTIONS_OTHER;
  return subs.reduce((acc, [re, glyph]) => acc.replace(re, glyph), s);
};

/**
 * Keyboard glyph atom.
 *
 *   <Kbd>Mod+K</Kbd>            → ⌘K (mac) or Ctrl+K (others)
 *   <Kbd>Shift+Enter</Kbd>      → ⇧↵
 *
 * The token "Mod" auto-resolves to the platform meta key.  Use it when
 * a shortcut is meant to be portable.
 */
export const Kbd: Component<KbdProps> = (props) => {
  const [local, rest] = splitProps(props, ["class", "children"]);
  return (
    <kbd
      class={[
        "inline-flex h-5 items-center rounded-sm border px-1.5",
        "border-border-subtle bg-bg-tertiary",
        "font-mono text-[0.7rem] font-medium text-text-secondary",
        local.class ?? "",
      ].join(" ")}
      {...rest}
    >
      {formatGlyphs(local.children)}
    </kbd>
  );
};
