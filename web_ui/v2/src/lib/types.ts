/**
 * Shared TypeScript types used across components + routes.
 */

export type ModeKey =
  | "research"
  | "thinking"
  | "build"
  | "consortium"
  | "sentinel"
  | "system"
  // Cycle UI 2026-05-20 — QuickCode is the auto-classifier's 7th
  // class (Decision 2 in the plan).  It surfaces in the unified
  // chat composer but is intentionally not in MODES below — the
  // sidebar's mode-color groups + the legacy /<mode> routes are
  // 6-mode shaped; QuickCode is composer-only.
  | "quickcode"
  // Cycle UI v2.9 — "chat" is the fast conversational lane (greetings,
  // chitchat, identity questions).  Composer-only like quickcode: not a
  // sidebar mode, label/colour come from i18n + mode-color fallback.
  | "chat";

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
  // Cycle UI 2026-05-20 — QuickCode entry for the composer's mode
  // picker.  href = /build/quick disambiguates from the canonical
  // Build mode but currently lands on the same /build route — the
  // backend's /api/quick-code/start endpoint is dispatched directly
  // by UnifiedChat based on the classifier verdict, not by routing.
  {
    key: "quickcode",
    label: "QuickCode",
    subtitle: "fast targeted edits",
    glyph: "zap",
    href: "/build",
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
  /** Cycle UI Phase 3 — which chat mode handled this turn (when
   *  known).  Populated by UnifiedChat when it dispatches to a
   *  mode-specific endpoint.  MessageBubble renders this as a
   *  small color-coded pill so users can see at a glance which
   *  pipeline produced each assistant message. */
  mode?: ModeKey;
  /** Cycle UI Phase 3 — classifier confidence snapshot from the
   *  moment this turn was submitted.  Optional; when present
   *  MessageBubble's hover surfaces top1/top2 scores for debug
   *  visibility during alpha. */
  classifierMeta?: {
    top1: string;
    top1_score: number;
    top2: string;
    top2_score: number;
    confidence: number;
    low_confidence: boolean;
  };
  /** Cycle UI v2.5 Phase 2 — Build-mode structured tool-call card.
   *  Set when the UNIFIED_REDUCER pushes a Build payload (code_ready /
   *  test_ready / execution_result / review_ready) as a separate
   *  ``role: "tool"`` turn rather than appending markdown to the
   *  assistant bubble.  ``MessageThread`` routes these to
   *  ``ToolCallCardBuild`` instead of ``MessageBubble``. */
  buildCard?: BuildToolCard;
  /** Cycle UI v2.7.1 — user-attached file refs persisted on the
   *  message.  Read-only render in MessageBubble (chip + download
   *  link to ``GET /api/attachments/{id}``).  Schema mirrors
   *  ``MessageAttachmentRef`` Pydantic model on the backend. */
  attachments?: ChatAttachmentRef[];
}

/** Cycle UI v2.7.1 — denormalized attachment ref persisted on chat
 *  messages.  Same shape backend writes via MessageAppendRequest
 *  + chat_messages.attachments[]. */
export interface ChatAttachmentRef {
  attachment_id: string;
  name: string;
  mime: string;
  size: number;
  role: "user_attached" | "model_emitted";
  inclusion: "inline_text" | "image_ref" | "filename_only";
  inline_preview?: string;
}

/** Cycle UI v2.5 Phase 2 — Build-engine tool-call payload.  One card
 *  per Build pipeline event (implement / test / execute / review).
 *  ``ToolCallCardBuild`` renders the header (file + kind), a
 *  collapsible body (DiffBlock for diff payloads, raw text otherwise),
 *  and a footer (status + duration_ms).
 *
 *  This is a domain-specific shape distinct from ``ToolCallFrame``
 *  (Vercel AI SDK tool-call event accumulator at
 *  ``web_ui/v2/src/lib/tool-stream.ts``).  Build's pipeline events
 *  don't share Vercel's input-stream semantics — they arrive as
 *  finished, atomic payloads — so a domain card is cleaner. */
export interface BuildToolCard {
  /** Which Build phase produced this card. */
  kind: "code_ready" | "test_ready" | "execution_result" | "review_ready";
  /** Primary file path the payload references (e.g. ``"snake.py"``).
   *  Optional — review_ready usually has no single file anchor. */
  file?: string;
  /** Detected language for syntax-highlighted body, or "diff" when
   *  the body is unified-diff text (DiffBlock renders). */
  language?: string;
  /** Card body — fenced code, unified diff, JSON output, etc. */
  body?: string;
  /** Status for the footer chip + colour. */
  status?: "ok" | "pending" | "failed" | "approved" | "needs_revision";
  /** Optional duration in milliseconds (sandbox runtime, etc.). */
  durationMs?: number;
  /** Free-form structured fields the card renders into the footer
   *  (e.g. ``{exit_code: 1}`` or ``{verdict_score: 7.2}``). */
  meta?: Record<string, unknown>;
}
