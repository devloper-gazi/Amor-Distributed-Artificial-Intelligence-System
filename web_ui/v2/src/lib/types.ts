/**
 * Shared TypeScript types used across components + routes.
 */

export type ModeKey =
  | "research"
  | "thinking"
  | "build"
  | "consortium"
  | "sentinel"
  | "system";

export interface ModeMeta {
  key: ModeKey;
  label: string;
  subtitle: string;
  /** Lucide-style glyph name (kept as a string so consumers can swap
   *  to whichever icon library they like). */
  glyph: string;
  /** Where in the v2 router this mode lives. */
  href: string;
  /** Whether this mode is wired end-to-end in v2.  Modes still
   *  staged for future PRs render an "Open in v1" callout. */
  wired: boolean;
}

export const MODES: ReadonlyArray<ModeMeta> = [
  {
    key: "research",
    label: "Research",
    subtitle: "gather, summarise, cite",
    glyph: "compass",
    href: "/research",
    wired: true,
  },
  {
    key: "build",
    label: "Build",
    subtitle: "code, test, debug",
    glyph: "hammer",
    href: "/build",
    wired: true,
  },
  {
    key: "thinking",
    label: "Thinking",
    subtitle: "multi-step reasoning",
    glyph: "brain",
    href: "/thinking",
    wired: true,
  },
  {
    key: "consortium",
    label: "Consortium",
    subtitle: "research + think + build",
    glyph: "users-round",
    href: "/consortium",
    wired: true,
  },
  {
    key: "sentinel",
    label: "Sentinel",
    subtitle: "governance, ledger",
    glyph: "shield-half",
    href: "/sentinel",
    wired: true,
  },
  {
    key: "system",
    label: "System",
    subtitle: "diagnostics, memory",
    glyph: "activity",
    href: "/system",
    wired: true,
  },
];

/** Conversation turn rendered in the message thread. */
export interface ChatTurn {
  id: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  /** Set when the turn is mid-stream so the UI can render a caret. */
  streaming?: boolean;
  /** Optional tag — e.g. "phase: implement" for pipeline turns. */
  tag?: string;
  ts?: number;
}
