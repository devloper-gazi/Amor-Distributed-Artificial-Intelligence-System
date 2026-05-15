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

import { RESEARCH_REDUCER, SIMPLE_TEXT_REDUCER } from "./chat-stream";


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
