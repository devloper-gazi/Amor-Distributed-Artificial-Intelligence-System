/**
 * Cycle C polish — RESEARCH_REDUCER unit coverage.
 *
 * The Research backend emits a rich event vocabulary that the old
 * ``SIMPLE_TEXT_REDUCER`` couldn't consume — the visible bug was
 * the assistant bubble showing the literal "(done)" instead of the
 * markdown report.  These tests pin the new reducer's contract:
 *
 * * Progress events (phase_start / sub_question / source_added /
 *   analyzing_source / relevance_filter) build a markdown trail
 *   that's user-readable inline.
 * * ``report_ready`` REPLACES the trail with the final markdown.
 * * ``done`` is a terminal signal and never carries text.
 * * Unknown / metadata events are silently dropped (no JSON
 *   regurgitation into the markdown).
 */

import { describe, expect, it } from "vitest";

import {
  RESEARCH_REDUCER,
  SIMPLE_TEXT_REDUCER,
  UNIFIED_REDUCER,
  normaliseErrorDetail,
} from "./chat-stream";


// ─── error detail normalisation ─────────────────────────────


describe("normaliseErrorDetail", () => {
  it("passes a plain string through", () => {
    expect(normaliseErrorDetail("boom")).toBe("boom");
  });

  it("flattens a FastAPI 422 detail array (no '[object Object]')", () => {
    const detail = [
      { loc: ["body", "goal"], msg: "Field required", type: "missing" },
      { loc: ["body", "depth"], msg: "too short", type: "value_error" },
    ];
    const out = normaliseErrorDetail(detail);
    expect(out).toBe("goal: Field required; depth: too short");
    expect(out).not.toContain("[object Object]");
  });

  it("reads .msg / .message off a single object", () => {
    expect(normaliseErrorDetail({ msg: "nope" })).toBe("nope");
    expect(normaliseErrorDetail({ message: "bad" })).toBe("bad");
  });

  it("returns null for null/empty so caller can fall back", () => {
    expect(normaliseErrorDetail(null)).toBeNull();
    expect(normaliseErrorDetail("")).toBeNull();
    expect(normaliseErrorDetail([])).toBeNull();
  });
});


// ─── progress events ────────────────────────────────────────


describe("RESEARCH_REDUCER — progress events", () => {
  it("phase_start emits an italic phase line + a tag", () => {
    const out = RESEARCH_REDUCER({ type: "phase_start", phase: "gathering" });
    expect(out).not.toBeNull();
    expect(out!.append).toBe("_— Phase: gathering_\n");
    expect(out!.tag).toBe("phase: gathering");
  });

  it("phase_start with empty phase is dropped", () => {
    const out = RESEARCH_REDUCER({ type: "phase_start" });
    expect(out).toBeNull();
  });

  it("phase_complete is swallowed — next phase_start is the visible signal", () => {
    expect(RESEARCH_REDUCER({ type: "phase_complete", phase: "gathering" })).toBeNull();
  });

  it("sub_question emits a numbered italic line", () => {
    const out = RESEARCH_REDUCER({
      type: "sub_question",
      index: 2,
      question: "What is the convergence guarantee?",
    });
    expect(out!.append).toBe(
      "_Sub-question 3: What is the convergence guarantee?_\n",
    );
  });

  it("source_added with url emits a markdown link", () => {
    const out = RESEARCH_REDUCER({
      type: "source_added",
      title: "CRDTs Illustrated",
      url: "https://example.com/crdt",
    });
    expect(out!.append).toBe(
      "_Source: [CRDTs Illustrated](https://example.com/crdt)_\n",
    );
  });

  it("source_added without url falls back to plain title", () => {
    const out = RESEARCH_REDUCER({
      type: "source_added",
      title: "An offline whitepaper",
    });
    expect(out!.append).toBe("_Source: An offline whitepaper_\n");
  });

  it("source_added with neither title nor url uses (untitled)", () => {
    const out = RESEARCH_REDUCER({ type: "source_added" });
    expect(out!.append).toBe("_Source: (untitled)_\n");
  });

  it("analyzing_source emits a counter line", () => {
    const out = RESEARCH_REDUCER({
      type: "analyzing_source",
      index: 2,
      total: 8,
      title: "Operational Transform retrospective",
    });
    expect(out!.append).toBe(
      "_Analyzing 3/8: Operational Transform retrospective_\n",
    );
  });

  it("relevance_filter reports drops + kept count", () => {
    const out = RESEARCH_REDUCER({
      type: "relevance_filter",
      filtered_out: 3,
      kept: 5,
    });
    expect(out!.append).toBe(
      "_Filtered 3 sources for relevance (kept 5)._\n",
    );
  });

  it("relevance_filter with zero filtered_out is silent", () => {
    expect(
      RESEARCH_REDUCER({ type: "relevance_filter", filtered_out: 0, kept: 5 }),
    ).toBeNull();
  });

  it("search_start / search_done / scrape_start are swallowed (chatty)", () => {
    expect(RESEARCH_REDUCER({ type: "search_start" })).toBeNull();
    expect(RESEARCH_REDUCER({ type: "search_done" })).toBeNull();
    expect(RESEARCH_REDUCER({ type: "scrape_start" })).toBeNull();
  });
});


// ─── report_ready (the canonical final-render path) ─────────


describe("RESEARCH_REDUCER — report_ready", () => {
  it("replaces the entire buffer with markdown", () => {
    const md = "# Final Report\n\nCRDTs win for offline-first sync.";
    const out = RESEARCH_REDUCER({ type: "report_ready", markdown: md });
    expect(out!.replace).toBe(md);
    expect(out!.tag).toBe("report");
    // ``replace`` is the explicit signal; ``done`` is NOT yet — the
    // backend emits a separate ``done`` event after the report.
    expect(out!.done).toBeUndefined();
  });

  it("falls back to ev.report when markdown field is missing", () => {
    const out = RESEARCH_REDUCER({ type: "report_ready", report: "# alt" });
    expect(out!.replace).toBe("# alt");
  });

  it("falls back to ev.content when markdown + report missing", () => {
    const out = RESEARCH_REDUCER({ type: "report_ready", content: "# fallback" });
    expect(out!.replace).toBe("# fallback");
  });

  it("with no markdown payload returns null (don't blank the trail)", () => {
    expect(RESEARCH_REDUCER({ type: "report_ready" })).toBeNull();
  });

  it("research_complete is treated identically (back-compat alias)", () => {
    const md = "# Same as report_ready";
    const out = RESEARCH_REDUCER({ type: "research_complete", markdown: md });
    expect(out!.replace).toBe(md);
  });
});


// ─── stream lifecycle ──────────────────────────────────────


describe("RESEARCH_REDUCER — terminators", () => {
  it("done emits the canonical done patch", () => {
    const out = RESEARCH_REDUCER({ type: "done" });
    expect(out).toEqual({ done: true, tag: "done" });
  });

  it("error surfaces the message", () => {
    const out = RESEARCH_REDUCER({ type: "error", message: "boom" });
    expect(out!.error).toBe("boom");
  });

  it("error falls back to ev.error then to a generic string", () => {
    expect(RESEARCH_REDUCER({ type: "error", error: "alt" })!.error).toBe("alt");
    expect(RESEARCH_REDUCER({ type: "error" })!.error).toBe("research error");
  });

  it("cancelled emits a cancellation patch", () => {
    expect(RESEARCH_REDUCER({ type: "cancelled" })).toEqual({
      tag: "cancelled",
      done: true,
    });
  });
});


// ─── snapshot replay ───────────────────────────────────────


describe("RESEARCH_REDUCER — snapshot", () => {
  it("snapshot with no events returns null (empty replay)", () => {
    expect(RESEARCH_REDUCER({ type: "snapshot", events: [] })).toBeNull();
  });

  it("snapshot replays phase + sub_question + source_added events as one append", () => {
    const out = RESEARCH_REDUCER({
      type: "snapshot",
      events: [
        { type: "phase_start", phase: "gathering" },
        { type: "sub_question", index: 0, question: "Q1?" },
        { type: "source_added", title: "Doc", url: "https://example.com" },
      ],
    });
    expect(out!.append).toContain("Phase: gathering");
    expect(out!.append).toContain("Sub-question 1: Q1?");
    expect(out!.append).toContain("[Doc](https://example.com)");
  });

  it("snapshot with a final report_ready short-circuits to replace", () => {
    const out = RESEARCH_REDUCER({
      type: "snapshot",
      events: [
        { type: "phase_start", phase: "gathering" },
        { type: "report_ready", markdown: "# Replay-friendly" },
      ],
    });
    expect(out!.replace).toBe("# Replay-friendly");
    expect(out!.tag).toBe("report");
  });
});


// ─── unknown / metadata events ──────────────────────────────


describe("RESEARCH_REDUCER — unknowns", () => {
  it("unknown event types are silently dropped", () => {
    expect(RESEARCH_REDUCER({ type: "model_download_start" })).toBeNull();
    expect(RESEARCH_REDUCER({ type: "totally_made_up", text: "hi" })).toBeNull();
  });

  it("malformed event with no type is dropped", () => {
    expect(RESEARCH_REDUCER({} as never)).toBeNull();
  });
});


// ─── parity check: SIMPLE_TEXT_REDUCER still works for non-Research ──


describe("SIMPLE_TEXT_REDUCER stays untouched for the modes that use it", () => {
  it("delta event still appends text", () => {
    const out = SIMPLE_TEXT_REDUCER({ type: "delta", text: "hello" });
    expect(out!.append).toBe("hello");
  });

  it("done still emits done", () => {
    expect(SIMPLE_TEXT_REDUCER({ type: "done" })).toEqual({
      done: true,
      tag: "done",
    });
  });
});


// ─── Cycle F Sprint 5 — approval_required handling ──────────


describe("SIMPLE_TEXT_REDUCER — approval_required", () => {
  it("pushes an approval turn with the SSE payload", () => {
    const out = SIMPLE_TEXT_REDUCER({
      type: "approval_required",
      request_id: "req-xyz",
      tool_name: "rm_rf",
      category: "delete",
      arguments: { path: "/tmp/x" },
      actor_role: "coder",
      timeout_s: 60,
    });
    expect(out).not.toBeNull();
    expect(out!.pushTurn).toBeDefined();
    expect(out!.pushTurn!.role).toBe("approval");
    expect(out!.pushTurn!.approval).toEqual({
      request_id: "req-xyz",
      tool_name: "rm_rf",
      category: "delete",
      arguments: { path: "/tmp/x" },
      actor_role: "coder",
      timeout_s: 60,
      status: "pending",
    });
    // Doesn't touch the assistant buffer at all.
    expect(out!.append).toBeUndefined();
    expect(out!.replace).toBeUndefined();
  });

  it("drops approval_required without request_id (defensive)", () => {
    const out = SIMPLE_TEXT_REDUCER({
      type: "approval_required",
      tool_name: "rm_rf",
    });
    expect(out).toBeNull();
  });

  it("defaults missing optional fields", () => {
    const out = SIMPLE_TEXT_REDUCER({
      type: "approval_required",
      request_id: "req-2",
    });
    expect(out).not.toBeNull();
    const approval = out!.pushTurn!.approval!;
    expect(approval.request_id).toBe("req-2");
    expect(approval.tool_name).toBe("unknown");
    expect(approval.category).toBe("unclassified");
    expect(approval.arguments).toEqual({});
    expect(approval.actor_role).toBeNull();
    expect(approval.timeout_s).toBe(90);
    expect(approval.status).toBe("pending");
  });

  it("rejects non-object arguments and falls back to {}", () => {
    const out = SIMPLE_TEXT_REDUCER({
      type: "approval_required",
      request_id: "req-3",
      arguments: "not a dict",
    });
    expect(out!.pushTurn!.approval!.arguments).toEqual({});
  });
});


// ─── Cycle UI Phase 3 — UNIFIED_REDUCER dispatch tests ───────────────


describe("UNIFIED_REDUCER — cross-cutting events", () => {
  it("approval_required → pushes an approval turn (delegates to SIMPLE)", () => {
    const out = UNIFIED_REDUCER({
      type: "approval_required",
      request_id: "req-1",
      tool_name: "file.write",
      category: "write",
      arguments: { path: "/tmp/foo" },
      actor_role: "coder",
      timeout_s: 60,
    });
    expect(out).not.toBeNull();
    expect(out!.pushTurn).toBeDefined();
    expect(out!.pushTurn!.role).toBe("approval");
    expect(out!.pushTurn!.approval!.request_id).toBe("req-1");
  });
  it("done → terminator with done=true", () => {
    const out = UNIFIED_REDUCER({ type: "done" });
    expect(out!.done).toBe(true);
    expect(out!.tag).toBe("done");
  });
  it("error → carries the error message", () => {
    const out = UNIFIED_REDUCER({ type: "error", message: "boom" });
    expect(out!.error).toBe("boom");
  });
  it("cancelled → terminator with cancelled tag", () => {
    const out = UNIFIED_REDUCER({ type: "cancelled" });
    expect(out!.tag).toBe("cancelled");
    expect(out!.done).toBe(true);
  });
  it("generic phase_start → updates tag, no content", () => {
    const out = UNIFIED_REDUCER({ type: "phase_start", phase: "implement" });
    expect(out!.tag).toBe("phase: implement");
    expect(out!.append).toBeUndefined();
  });
});

describe("UNIFIED_REDUCER — Build-specific dispatches (Cycle UI v2.5 Phase 2)", () => {
  // Cycle UI v2.5 Phase 2 — Build typed events now emit STRUCTURED
  // ToolCallCardBuild pushTurns rather than appending markdown
  // fences to the assistant bubble.  Pin the new shape so we catch
  // future regressions (e.g. someone reintroduces the legacy
  // `append:` path).
  it("code_ready → pushes a Build tool turn with buildCard payload", () => {
    const out = UNIFIED_REDUCER({
      type: "code_ready",
      code: "console.log('hi');",
      language: "javascript",
      file: "snake.js",
    });
    expect(out).not.toBeNull();
    expect(out!.pushTurn).toBeDefined();
    expect(out!.pushTurn!.role).toBe("tool");
    expect(out!.pushTurn!.buildCard).toBeDefined();
    expect(out!.pushTurn!.buildCard!.kind).toBe("code_ready");
    expect(out!.pushTurn!.buildCard!.file).toBe("snake.js");
    expect(out!.pushTurn!.buildCard!.language).toBe("javascript");
    expect(out!.pushTurn!.buildCard!.body).toContain("```javascript");
    expect(out!.pushTurn!.buildCard!.body).toContain("console.log");
    expect(out!.pushTurn!.buildCard!.status).toBe("ok");
    expect(out!.tag).toBe("code");
  });
  it("test_ready → pushes a Build tool turn for tests", () => {
    const out = UNIFIED_REDUCER({
      type: "test_ready",
      tests: "assert(1 === 1);",
      language: "javascript",
    });
    expect(out).not.toBeNull();
    expect(out!.pushTurn!.buildCard!.kind).toBe("test_ready");
    expect(out!.pushTurn!.buildCard!.body).toContain("assert(1 === 1);");
    expect(out!.pushTurn!.buildCard!.language).toBe("javascript");
    expect(out!.tag).toBe("test");
  });
  it("execution_result(passed=true) → status=ok, no error body", () => {
    const out = UNIFIED_REDUCER({
      type: "execution_result",
      passed: true,
      stdout: "ok",
      duration_ms: 123,
    });
    expect(out!.pushTurn!.buildCard!.kind).toBe("execution_result");
    expect(out!.pushTurn!.buildCard!.status).toBe("ok");
    expect(out!.pushTurn!.buildCard!.durationMs).toBe(123);
    expect(out!.tag).toBe("executed");
  });
  it("execution_result(passed=false) → status=failed, body includes stderr", () => {
    const out = UNIFIED_REDUCER({
      type: "execution_result",
      passed: false,
      stdout: "trace",
      stderr: "ImportError: foo",
      exit_code: 1,
    });
    expect(out!.pushTurn!.buildCard!.status).toBe("failed");
    expect(out!.pushTurn!.buildCard!.body).toContain("ImportError: foo");
    expect(out!.pushTurn!.buildCard!.body).toContain("stderr:");
    expect(out!.pushTurn!.buildCard!.meta).toEqual({ exit_code: 1 });
    expect(out!.tag).toBe("execution_failed");
  });
  it("review_ready → both replaces buffer AND pushes a build tool turn", () => {
    const out = UNIFIED_REDUCER({
      type: "review_ready",
      verdict: "approved",
      markdown: "# Review\n\nLGTM",
    });
    // Replace into the assistant bubble — review IS the final
    // deliverable for Build sessions.
    expect(out!.replace).toContain("LGTM");
    expect(out!.tag).toContain("approved");
    // PLUS an additive pushTurn so the user sees a structured
    // review card with the verdict status chip.
    expect(out!.pushTurn).toBeDefined();
    expect(out!.pushTurn!.buildCard!.kind).toBe("review_ready");
    expect(out!.pushTurn!.buildCard!.status).toBe("approved");
    expect(out!.pushTurn!.buildCard!.body).toContain("LGTM");
  });
  it("model_download_progress → tag-only update", () => {
    const out = UNIFIED_REDUCER({
      type: "model_download_progress",
      model: "qwen2.5-coder:7b",
      percent: 42,
    });
    expect(out!.tag).toContain("downloading");
    expect(out!.tag).toContain("qwen2.5-coder:7b");
    expect(out!.append).toBeUndefined();
  });
  it("language_corrected → silent tag update", () => {
    const out = UNIFIED_REDUCER({
      type: "language_corrected",
      label: "rust → python",
    });
    expect(out!.tag).toBe("rust → python");
    expect(out!.append).toBeUndefined();
  });
});

describe("UNIFIED_REDUCER — delegates to RESEARCH_REDUCER for Research events", () => {
  it("source_added → appends italic source line", () => {
    const out = UNIFIED_REDUCER({
      type: "source_added",
      title: "Paper X",
      url: "https://example.com",
    });
    expect(out).not.toBeNull();
    expect(out!.append).toContain("Source:");
    expect(out!.append).toContain("Paper X");
    expect(out!.tag).toBe("research");
  });
  it("report_ready → replaces buffer with markdown report", () => {
    const out = UNIFIED_REDUCER({
      type: "report_ready",
      markdown: "## Final report",
    });
    expect(out!.replace).toContain("Final report");
    expect(out!.tag).toBe("report");
  });
});

describe("UNIFIED_REDUCER — text-chunk fallback works across modes", () => {
  it("thinking_chunk → appends content", () => {
    const out = UNIFIED_REDUCER({
      type: "thinking_chunk",
      text: "step 1: ",
    });
    expect(out!.append).toBe("step 1: ");
  });
  it("consortium chunk → appends content", () => {
    const out = UNIFIED_REDUCER({
      type: "chunk",
      content: "agent says hi",
    });
    expect(out!.append).toBe("agent says hi");
  });
});

describe("UNIFIED_REDUCER — unknown event handling", () => {
  it("unknown type with no payload → null (silent drop)", () => {
    const out = UNIFIED_REDUCER({ type: "completely_made_up_event_xyz" });
    expect(out).toBeNull();
  });
  it("unknown type with text payload → salvages it", () => {
    const out = UNIFIED_REDUCER({
      type: "future_event",
      text: "hi from the future",
    });
    expect(out!.append).toBe("hi from the future");
  });
});
