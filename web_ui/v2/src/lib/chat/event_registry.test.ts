/**
 * Cycle UI Phase 3 — event_registry tests.
 *
 * Validates the discriminated-union / category-dispatch logic that
 * UNIFIED_REDUCER depends on.  Pure data tests — no Solid signals.
 */

import { describe, it, expect } from "vitest";
import {
  ALL_MODES,
  BUILD_EVENTS,
  CONSORTIUM_EVENTS,
  CROSS_CUTTING_EVENTS,
  QUICKCODE_EVENTS,
  RESEARCH_EVENTS,
  SENTINEL_EVENTS,
  THINKING_EVENTS,
  extractText,
  getEventCategory,
  isCrossCutting,
  isFinalDeliverable,
  isTerminator,
  isTextChunk,
  normaliseModeKey,
} from "./event_registry";

// ─── ALL_MODES + class sets ─────────────────────────────────────────

describe("ALL_MODES", () => {
  it("is exactly the 6 expected classes", () => {
    expect([...ALL_MODES].sort()).toEqual([
      "build", "consortium", "quickcode", "research", "sentinel", "thinking",
    ]);
  });
});

describe("per-mode event sets", () => {
  it("BUILD_EVENTS contain the canonical Build payloads", () => {
    expect(BUILD_EVENTS).toContain("code_ready");
    expect(BUILD_EVENTS).toContain("test_ready");
    expect(BUILD_EVENTS).toContain("execution_result");
    expect(BUILD_EVENTS).toContain("review_ready");
  });
  it("RESEARCH_EVENTS contain the canonical Research payloads", () => {
    expect(RESEARCH_EVENTS).toContain("sub_question");
    expect(RESEARCH_EVENTS).toContain("source_added");
    expect(RESEARCH_EVENTS).toContain("analyzing_source");
    expect(RESEARCH_EVENTS).toContain("report_ready");
  });
  it("QUICKCODE_EVENTS aliases BUILD_EVENTS (same vocabulary)", () => {
    expect(QUICKCODE_EVENTS).toBe(BUILD_EVENTS);
  });
  it("THINKING_EVENTS include chunk + delta + thinking_chunk", () => {
    expect(THINKING_EVENTS).toContain("thinking_chunk");
    expect(THINKING_EVENTS).toContain("chunk");
    expect(THINKING_EVENTS).toContain("delta");
  });
  it("CONSORTIUM_EVENTS expose artifact_ready", () => {
    expect(CONSORTIUM_EVENTS).toContain("artifact_ready");
    expect(CONSORTIUM_EVENTS).toContain("consortium_completed");
  });
  it("SENTINEL_EVENTS expose finding_ready", () => {
    expect(SENTINEL_EVENTS).toContain("finding_ready");
    expect(SENTINEL_EVENTS).toContain("sentinel_completed");
  });
  it("CROSS_CUTTING_EVENTS include the terminal trio + approval pair", () => {
    expect(CROSS_CUTTING_EVENTS).toContain("done");
    expect(CROSS_CUTTING_EVENTS).toContain("error");
    expect(CROSS_CUTTING_EVENTS).toContain("cancelled");
    expect(CROSS_CUTTING_EVENTS).toContain("approval_required");
    expect(CROSS_CUTTING_EVENTS).toContain("approval_resolved");
    expect(CROSS_CUTTING_EVENTS).toContain("snapshot");
  });
});

// ─── getEventCategory ───────────────────────────────────────────────

describe("getEventCategory", () => {
  it("returns null for cross-cutting events", () => {
    expect(getEventCategory("done")).toBeNull();
    expect(getEventCategory("error")).toBeNull();
    expect(getEventCategory("approval_required")).toBeNull();
    expect(getEventCategory("phase_start")).toBeNull();
    expect(getEventCategory("snapshot")).toBeNull();
  });
  it("routes Build events to build", () => {
    expect(getEventCategory("code_ready")).toBe("build");
    expect(getEventCategory("test_ready")).toBe("build");
    expect(getEventCategory("execution_result")).toBe("build");
    expect(getEventCategory("review_ready")).toBe("build");
  });
  it("routes Research events to research", () => {
    expect(getEventCategory("sub_question")).toBe("research");
    expect(getEventCategory("source_added")).toBe("research");
    expect(getEventCategory("report_ready")).toBe("research");
  });
  it("routes Thinking events to thinking", () => {
    expect(getEventCategory("thinking_chunk")).toBe("thinking");
  });
  it("routes Consortium events to consortium", () => {
    expect(getEventCategory("artifact_ready")).toBe("consortium");
    expect(getEventCategory("consortium_completed")).toBe("consortium");
  });
  it("routes Sentinel events to sentinel", () => {
    expect(getEventCategory("finding_ready")).toBe("sentinel");
  });
  it("returns null for unknown events", () => {
    expect(getEventCategory("some_made_up_event")).toBeNull();
    expect(getEventCategory("")).toBeNull();
  });
});

// ─── isCrossCutting ────────────────────────────────────────────────

describe("isCrossCutting", () => {
  it("returns true for terminal + approval + snapshot + generic phase_*", () => {
    for (const t of ["done", "error", "cancelled", "approval_required",
                     "approval_resolved", "snapshot", "phase_start",
                     "phase_complete", "phase_failed"]) {
      expect(isCrossCutting(t)).toBe(true);
    }
  });
  it("returns false for mode-specific events", () => {
    for (const t of ["code_ready", "sub_question", "thinking_chunk",
                     "artifact_ready", "finding_ready",
                     "consortium_phase_start"]) {
      expect(isCrossCutting(t)).toBe(false);
    }
  });
});

// ─── isTerminator ──────────────────────────────────────────────────

describe("isTerminator", () => {
  it("recognises bare + mode-prefixed terminators", () => {
    expect(isTerminator("done")).toBe(true);
    expect(isTerminator("error")).toBe(true);
    expect(isTerminator("cancelled")).toBe(true);
    expect(isTerminator("consortium_completed")).toBe(true);
    expect(isTerminator("sentinel_completed")).toBe(true);
    expect(isTerminator("build_error")).toBe(true);
    expect(isTerminator("research_cancelled")).toBe(true);
  });
  it("rejects mid-stream events", () => {
    expect(isTerminator("chunk")).toBe(false);
    expect(isTerminator("code_ready")).toBe(false);
    expect(isTerminator("phase_start")).toBe(false);
  });
});

// ─── isFinalDeliverable ─────────────────────────────────────────────

describe("isFinalDeliverable", () => {
  it("recognises the canonical final-payload event types", () => {
    expect(isFinalDeliverable("report_ready")).toBe(true);
    expect(isFinalDeliverable("research_complete")).toBe(true);
    expect(isFinalDeliverable("deliverable_ready")).toBe(true);
    expect(isFinalDeliverable("review_ready")).toBe(true);
    expect(isFinalDeliverable("consortium_completed")).toBe(true);
  });
  it("rejects mid-stream + terminator-only events", () => {
    expect(isFinalDeliverable("done")).toBe(false);
    expect(isFinalDeliverable("error")).toBe(false);
    expect(isFinalDeliverable("code_ready")).toBe(false);
    expect(isFinalDeliverable("chunk")).toBe(false);
  });
});

// ─── isTextChunk ────────────────────────────────────────────────────

describe("isTextChunk", () => {
  it("recognises every chunk variant", () => {
    expect(isTextChunk("chunk")).toBe(true);
    expect(isTextChunk("delta")).toBe(true);
    expect(isTextChunk("text_chunk")).toBe(true);
    expect(isTextChunk("research_chunk")).toBe(true);
    expect(isTextChunk("synthesis_chunk")).toBe(true);
    expect(isTextChunk("thinking_chunk")).toBe(true);
  });
  it("rejects payload-shaped events", () => {
    expect(isTextChunk("code_ready")).toBe(false);
    expect(isTextChunk("report_ready")).toBe(false);
    expect(isTextChunk("done")).toBe(false);
  });
});

// ─── extractText ────────────────────────────────────────────────────

describe("extractText", () => {
  it("prefers text field when present", () => {
    expect(extractText({ text: "hello", content: "world" })).toBe("hello");
  });
  it("falls back through content, markdown, report, summary, message", () => {
    expect(extractText({ content: "a" })).toBe("a");
    expect(extractText({ markdown: "b" })).toBe("b");
    expect(extractText({ report: "c" })).toBe("c");
    expect(extractText({ summary: "d" })).toBe("d");
    expect(extractText({ message: "e" })).toBe("e");
  });
  it("returns empty string when no recognised key", () => {
    expect(extractText({})).toBe("");
    expect(extractText({ unknown: "field" })).toBe("");
    expect(extractText({ text: 42 } as unknown as Record<string, unknown>)).toBe("");
  });
  it("treats empty-string text as absent and falls through", () => {
    expect(extractText({ text: "", content: "fallback" })).toBe("fallback");
  });
});

// ─── normaliseModeKey ──────────────────────────────────────────────

describe("normaliseModeKey", () => {
  it("maps backend 'code' alias to canonical 'build'", () => {
    expect(normaliseModeKey("code")).toBe("build");
    expect(normaliseModeKey("CODE")).toBe("build");
  });
  it("passes canonical mode keys through unchanged", () => {
    for (const m of ALL_MODES) {
      expect(normaliseModeKey(m)).toBe(m);
    }
  });
  it("normalises whitespace + case", () => {
    expect(normaliseModeKey("  Research  ")).toBe("research");
    expect(normaliseModeKey("BUILD")).toBe("build");
  });
  it("returns null for unknown modes", () => {
    expect(normaliseModeKey("system")).toBeNull();  // not in 6-class set
    expect(normaliseModeKey("foo")).toBeNull();
    expect(normaliseModeKey("")).toBeNull();
  });
});
