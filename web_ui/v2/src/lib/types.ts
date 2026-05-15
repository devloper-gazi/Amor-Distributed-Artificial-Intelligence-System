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

/** Cycle F Sprint 5 — approval-request payload threaded inside a
 *  ChatTurn so it renders as an inline approval card.  The card
 *  manages its own resolution state via the
 *  POST /api/approval/{request_id} endpoint; the chat-stream
 *  reducer only creates the turn on the SSE `approval_required`
 *  event. */
export interface ApprovalPayload {
  request_id: string;
  tool_name: string;
  category: string;        // ApprovalCategory enum value
  arguments: Record<string, unknown>;
  actor_role?: string | null;
  timeout_s: number;
  /** Local UI state; transitions from "pending" via the POST. */
  status: "pending" | "approved" | "denied" | "timeout" | "error";
  /** Optional free-text error surfaced on POST failure. */
  error?: string;
}

/** Conversation turn rendered in the message thread. */
export interface ChatTurn {
  id: string;
  role: "user" | "assistant" | "tool" | "system" | "approval";
  content: string;
  /** Set when the turn is mid-stream so the UI can render a caret. */
  streaming?: boolean;
  /** Optional tag — e.g. "phase: implement" for pipeline turns. */
  tag?: string;
  ts?: number;
  /** Cycle C Sprint 7 — when set, the turn was generated with
   *  Mem0-injected context.  ``count`` is how many memories the
   *  retriever surfaced.  ``snippets`` (optional) is the short
   *  text the "Remembered" pill expands to on hover. */
  remembered?: {
    count: number;
    snippets?: string[];
  };
  /** Cycle F Sprint 5 — populated when role === "approval".  The
   *  MessageThread switches to ApprovalPrompt.tsx for these turns
   *  instead of MessageBubble. */
  approval?: ApprovalPayload;
}
