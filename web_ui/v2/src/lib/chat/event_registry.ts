/**
 * Cycle UI Phase 3 — Single source of truth for AMOR's SSE event_type
 * taxonomy across all 6 chat modes.
 *
 * AMOR's wire format is `{type: string, event_id?: string, ...extra}`
 * (see `web_ui/v2/src/lib/sse.ts`).  Backend publishers in
 * `document_processor/api/{code_intelligence,chat_research,thinking,
 * consortium,sentinel,quick_code}_routes.py` each emit a slightly
 * different vocabulary.  Until this registry was formalised the
 * frontend had two parallel reducers (SIMPLE_TEXT_REDUCER +
 * RESEARCH_REDUCER) plus a third inline one in routes/Build.tsx,
 * each duplicating event-type checks.
 *
 * This module is pure data + helpers — no Solid signals, no DOM.  It
 * powers the UNIFIED_REDUCER in `chat-stream.ts` (Phase 3.2) and is
 * test-target for `event_registry.test.ts`.
 *
 * Forward-compat: an event type unknown to this registry is NOT a
 * fatal — the reducer falls back to rendering the payload's `.text`
 * or `.content` field when present, or silently swallows the event.
 * This matches the documented contract that the wire format stays
 * stable-ish but new event types may appear in any release.
 */

/* ─── Mode keys ─────────────────────────────────────────────────── */

/** The 6 backend mode endpoints UnifiedChat dispatches to.  Kept in
 *  sync with `web_ui/v2/src/lib/intent-classifier.ts:ChatMode` and
 *  `document_processor/services/intent_classifier.py:CLASSES`. */
export type ChatMode =
  | "build"
  | "research"
  | "thinking"
  | "consortium"
  | "sentinel"
  | "quickcode"
  // Cycle UI v2.9 — fast conversational lane (greetings / chitchat).
  // Reuses the generic ``text_chunk`` + ``done`` cross-cutting events;
  // no mode-specific event catalogue needed.
  | "chat";

export const ALL_MODES: readonly ChatMode[] = [
  "build",
  "research",
  "thinking",
  "consortium",
  "sentinel",
  "quickcode",
  "chat",
] as const;

/* ─── Cross-cutting event types ─────────────────────────────────── */

/** Events every backend can emit irrespective of mode.  The reducer
 *  handles these BEFORE branching on the per-mode logic — keeps the
 *  approval flow + final-done handling uniform across modes. */
export const CROSS_CUTTING_EVENTS = [
  "snapshot",            // stream-open state replay (Cycle B/D)
  "approval_required",   // Cycle F approval bridge
  "approval_resolved",   // Cycle F approval bridge resolution
  "done",                // stream terminal — success
  "error",               // stream terminal — failure
  "cancelled",           // stream terminal — user cancelled
  "phase_start",         // bare phase_start (mode-prefix optional)
  "phase_complete",
  "phase_failed",
] as const;

export type CrossCuttingEventType =
  (typeof CROSS_CUTTING_EVENTS)[number];

/* ─── Per-mode event-type catalogues ────────────────────────────── */

/** Events the Build / Code Intelligence engine emits in addition to
 *  the cross-cutting set.  The engine has 9 named phases (triage →
 *  model_prep → plan → implement → execute → analyze → test → debug
 *  → review) that fire `phase_start` events; the typed events listed
 *  here carry the actual deliverable payloads. */
export const BUILD_EVENTS = [
  "code_ready",          // implement phase produced source
  "test_ready",          // tests written
  "execution_result",    // sandbox run finished
  "review_ready",        // review phase verdict
  "model_download_progress",
  "language_corrected",
  "planner_fallback",
  "install_packages_filtered",
  "diff_block",          // (Sprint 5) per-file diff payload
  "deliverable_ready",   // generic — also research/thinking
] as const;

/** Research advanced_researcher events — 6-phase pipeline plus
 *  fine-grained sub_question + source-tracking events used by
 *  RESEARCH_REDUCER to render an italic progress trail above the
 *  final markdown report. */
export const RESEARCH_EVENTS = [
  "sub_question",
  "search_start",
  "search_done",
  "scrape_start",
  "source_added",
  "analyzing_source",
  "relevance_filter",
  "report_ready",          // final markdown
  "research_complete",     // alias for report_ready (legacy)
  "research_chunk",
  "research_snapshot",
] as const;

/** Thinking mode events — multi-step reasoning with explicit
 *  step markers.  The chunk variants carry token deltas. */
export const THINKING_EVENTS = [
  "thinking_chunk",
  "chunk",
  "delta",
  "synthesis_chunk",
  "thinking_complete",
  "deliverable_ready",
] as const;

/** Consortium mode events — multi-agent panel with per-agent
 *  contributions + a final consortium_completed verdict. */
export const CONSORTIUM_EVENTS = [
  "artifact_ready",
  "consortium_completed",
  "consortium_phase_start",
  "consortium_phase_complete",
  "consortium_phase_failed",
] as const;

/** Sentinel mode events — security/governance findings stream. */
export const SENTINEL_EVENTS = [
  "finding_ready",
  "sentinel_completed",
  "sentinel_phase_start",
  "sentinel_phase_complete",
  "sentinel_phase_failed",
] as const;

/** QuickCode events — fast Build variant; uses the same phase
 *  vocabulary as Build but mostly emits code_ready in 1-2 phases. */
export const QUICKCODE_EVENTS = BUILD_EVENTS;

/* ─── Combined union + per-mode dispatch table ──────────────────── */

export type ChatEventType =
  | CrossCuttingEventType
  | (typeof BUILD_EVENTS)[number]
  | (typeof RESEARCH_EVENTS)[number]
  | (typeof THINKING_EVENTS)[number]
  | (typeof CONSORTIUM_EVENTS)[number]
  | (typeof SENTINEL_EVENTS)[number];

/** Lookup: which mode owns this event type?  Cross-cutting events
 *  return null (they're handled before per-mode branches in the
 *  UNIFIED_REDUCER).  Unknown types return null as well so callers
 *  can use `getEventCategory(t) === null` to decide whether to
 *  attempt mode-specific handling vs. fall through to the generic
 *  text-passthrough branch. */
export function getEventCategory(type: string): ChatMode | null {
  if ((CROSS_CUTTING_EVENTS as readonly string[]).includes(type)) {
    // Cross-cutting events are mode-agnostic — return null to flag
    // the reducer to use the cross-cutting branch.
    return null;
  }
  if ((BUILD_EVENTS as readonly string[]).includes(type)) return "build";
  if ((RESEARCH_EVENTS as readonly string[]).includes(type)) return "research";
  if ((THINKING_EVENTS as readonly string[]).includes(type)) return "thinking";
  if ((CONSORTIUM_EVENTS as readonly string[]).includes(type)) return "consortium";
  if ((SENTINEL_EVENTS as readonly string[]).includes(type)) return "sentinel";
  // Mode-prefixed phases (e.g., "consortium_phase_start") matched
  // above via the explicit lists; the generic phase_* matched
  // via CROSS_CUTTING_EVENTS.  Anything else is unknown.
  return null;
}

/** True when the event is one the reducer's cross-cutting branch
 *  should handle directly (terminator, approval, snapshot, generic
 *  phase markers).  Mode-prefixed phase events (e.g.
 *  ``consortium_phase_start``) are NOT cross-cutting — they belong
 *  to a specific mode's branch. */
export function isCrossCutting(type: string): boolean {
  return (CROSS_CUTTING_EVENTS as readonly string[]).includes(type);
}

/** True when this type is a known terminator (done / error /
 *  cancelled or any mode-prefixed variant).  Used by UNIFIED_REDUCER
 *  to decide whether to set the patch's ``done`` flag. */
export function isTerminator(type: string): boolean {
  if (type === "done" || type === "error" || type === "cancelled") return true;
  if (type.endsWith("_completed")) return true;
  if (type.endsWith("_error")) return true;
  if (type.endsWith("_cancelled")) return true;
  return false;
}

/** True when this type is a "final deliverable" — should REPLACE
 *  the assistant turn buffer with the payload's full markdown
 *  rather than appending to it.  Distinct from terminators because
 *  some terminators (plain ``done``) don't carry content. */
export function isFinalDeliverable(type: string): boolean {
  return (
    type === "report_ready" ||
    type === "research_complete" ||
    type === "deliverable_ready" ||
    type === "thinking_complete" ||
    type === "consortium_completed" ||
    type === "sentinel_completed" ||
    type === "review_ready" ||  // Build's final review verdict
    type === "complete"           // legacy alias
  );
}

/** True when this type is a streaming-text chunk — appends to the
 *  current assistant turn buffer.  Covers the various per-mode
 *  chunk variants the backend emits. */
export function isTextChunk(type: string): boolean {
  return (
    type === "chunk" ||
    type === "delta" ||
    type === "text_chunk" ||
    type === "research_chunk" ||
    type === "synthesis_chunk" ||
    type === "thinking_chunk"
  );
}

/* ─── Helpers for the reducer ───────────────────────────────────── */

/** Extract the most-likely text payload from an event of unknown
 *  shape.  Tries common keys (text, content, markdown, report,
 *  summary, message) and returns the first non-empty string. */
export function extractText(ev: Record<string, unknown>): string {
  for (const key of ["text", "content", "markdown", "report", "summary", "message"]) {
    const v = ev[key];
    if (typeof v === "string" && v.length > 0) return v;
  }
  return "";
}

/** Map a backend mode (``"code"``) to the canonical ChatMode key
 *  used everywhere in the frontend (``"build"``).  Defensive — the
 *  backend has historically used both naming conventions. */
export function normaliseModeKey(rawMode: string): ChatMode | null {
  const normalised = rawMode.toLowerCase().trim();
  if (normalised === "code") return "build";
  if ((ALL_MODES as readonly string[]).includes(normalised)) {
    return normalised as ChatMode;
  }
  return null;
}
