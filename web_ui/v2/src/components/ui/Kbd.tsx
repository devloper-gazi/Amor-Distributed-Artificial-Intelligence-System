import { type Component, type JSX, splitProps } from "solid-js";

export interface KbdProps extends JSX.HTMLAttributes<HTMLElement> {
  /** Auto-substitutes ``Mod`` for the platform-correct meta key glyph
   *  (``Ã¢Å’Ëœ`` on macOS, ``Ctrl`` everywhere else). */
  children: string;
}

const isMac = () =>
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/.test(navigator.platform);

const SUBSTITUTIONS_MAC: Array<[RegExp, string]> = [
  [/\bMod\b/g, "Ã¢Å’Ëœ"],
  [/\bCmd\b/g, "Ã¢Å’Ëœ"],
  [/\bShift\b/g, "Ã¢â€¡Â§"],
  [/\bAlt\b/g, "Ã¢Å’Â¥"],
  [/\bCtrl\b/g, "Ã¢Å’Æ’"],
  [/\bEnter\b/g, "Ã¢â€ Âµ"],
  [/\bEsc\b/g, "Esc"],
  [/\bArrowUp\b/g, "Ã¢â€ â€˜"],
  [/\bArrowDown\b/g, "Ã¢â€ â€œ"],
  [/\bArrowLeft\b/g, "Ã¢â€ Â"],
  [/\bArrowRight\b/g, "Ã¢â€ â€™"],
];

const SUBSTITUTIONS_OTHER: Array<[RegExp, string]> = [
  [/\bMod\b/g, "Ctrl"],
  [/\bShift\b/g, "Shift"],
  [/\bAlt\b/g, "Alt"],
  [/\bEnter\b/g, "Ã¢â€ Âµ"],
  [/\bEsc\b/g, "Esc"],
  [/\bArrowUp\b/g, "Ã¢â€ â€˜"],
  [/\bArrowDown\b/g, "Ã¢â€ â€œ"],
  [/\bArrowLeft\b/g, "Ã¢â€ Â"],
  [/\bArrowRight\b/g, "Ã¢â€ â€™"],
];

const formatGlyphs = (s: string): string => {
  const subs = isMac() ? SUBSTITUTIONS_MAC : SUBSTITUTIONS_OTHER;
  return subs.reduce((acc, [re, glyph]) => acc.replace(re, glyph), s);
};

/**
 * Keyboard glyph atom.
 *
 *   <Kbd>Mod+K</Kbd>            Ã¢â€ â€™ Ã¢Å’ËœK (mac) or Ctrl+K (others)
 *   <Kbd>Shift+Enter</Kbd>      Ã¢â€ â€™ Ã¢â€¡Â§Ã¢â€ Âµ
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
        "border-border-subtle bg-bg-elevated-v25",
        "font-mono text-[0.7rem] font-medium text-text-body",
        local.class ?? "",
      ].join(" ")}
      {...rest}
    >
      {formatGlyphs(local.children)}
    </kbd>
  );
};
