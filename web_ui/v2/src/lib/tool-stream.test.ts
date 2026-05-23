/**
 * Cycle C Sprint 4 Day 4 — pure adapter tests for the legacy AMOR ↔
 * Vercel AI SDK 5–compatible tool-call envelope.
 *
 * No DOM, no Solid.  Vitest in node env exercises the adapter +
 * accumulator end-to-end.  See ``docs/sse-protocol.md`` for the
 * mapping contract these tests pin.
 */

import { describe, it, expect } from "vitest";
import {
  toToolEvents,
  ingestToolEvent,
  type ToolCallFrame,
  type AmorEvent,
} from "./tool-stream";

describe("toToolEvents", () => {
  it("returns nothing for non-tool events", () => {
    expect(toToolEvents({ type: "phase_start", phase: "triage" })).toEqual([]);
    expect(toToolEvents({ type: "done" })).toEqual([]);
  });

  it("passes canonical tool-* events through unchanged", () => {
    const ev: AmorEvent = {
      type: "tool-input-start",
      toolCallId: "x-1",
      tool: "sandbox-execute",
    };
    const out = toToolEvents(ev);
    expect(out).toHaveLength(1);
    expect(out[0]!.type).toBe("tool-input-start");
  });

  it("execution_start opens a sandbox-execute call", () => {
    const out = toToolEvents({
      type: "execution_start",
      iteration: 0,
      language: "python",
    });
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      type: "tool-input-start",
      tool: "sandbox-execute",
      toolCallId: "sandbox-execute-0",
    });
  });

  it("execution_install_packages emits an input delta", () => {
    const out = toToolEvents({
      type: "execution_install_packages",
      iteration: 0,
      packages: ["numpy", "pandas"],
    });
    expect(out[0]).toMatchObject({
      type: "tool-input-delta",
      toolCallId: "sandbox-execute-0",
    });
    expect((out[0] as { delta: string }).delta).toContain("numpy");
  });

  it("execution_result emits both input-available + output-available", () => {
    const out = toToolEvents({
      type: "execution_result",
      iteration: 0,
      language: "python",
      exit_code: 0,
      stdout: "hello",
      stderr: "",
      duration_ms: 42,
    });
    expect(out).toHaveLength(2);
    expect(out[0]!.type).toBe("tool-input-available");
    expect(out[1]!.type).toBe("tool-output-available");
    expect(
      (out[1] as { isError?: boolean }).isError,
    ).toBeFalsy();
  });

  it("non-zero exit code marks the output as an error", () => {
    const out = toToolEvents({
      type: "execution_result",
      iteration: 1,
      exit_code: 1,
      stdout: "",
      stderr: "boom",
    });
    const last = out[out.length - 1] as { isError?: boolean };
    expect(last.isError).toBe(true);
  });

  it("review_ready maps to a code-review tool call", () => {
    const out = toToolEvents({
      type: "review_ready",
      iteration: 0,
      score: 75,
      summary: "ok",
      findings: [],
    });
    expect(out[0]!.type).toBe("tool-input-start");
    expect((out[0] as { tool: string }).tool).toBe("code-review");
    expect(out[1]!.type).toBe("tool-output-available");
  });

  it("repomap_attached emits output only (no input)", () => {
    const out = toToolEvents({
      type: "repomap_attached",
      tokens_estimate: 256,
      render_ms: 25,
      budget_tokens: 2048,
    });
    expect(out).toHaveLength(1);
    expect(out[0]!.type).toBe("tool-output-available");
  });

  it("scopes call ids by iteration so retries get separate cards", () => {
    const a = toToolEvents({ type: "execution_start", iteration: 0 });
    const b = toToolEvents({ type: "execution_start", iteration: 1 });
    expect((a[0] as { toolCallId: string }).toolCallId).not.toBe(
      (b[0] as { toolCallId: string }).toolCallId,
    );
  });
});

describe("ingestToolEvent", () => {
  it("opens a frame on tool-input-start", () => {
    const m = ingestToolEvent(new Map(), {
      type: "tool-input-start",
      toolCallId: "x",
      tool: "t",
    });
    expect(m.get("x")).toMatchObject<ToolCallFrame>({
      id: "x",
      tool: "t",
      status: "running",
      inputDelta: "",
    });
  });

  it("appends deltas to inputDelta", () => {
    let m = ingestToolEvent(new Map(), {
      type: "tool-input-start",
      toolCallId: "x",
      tool: "t",
    });
    m = ingestToolEvent(m, {
      type: "tool-input-delta",
      toolCallId: "x",
      delta: "abc",
    });
    m = ingestToolEvent(m, {
      type: "tool-input-delta",
      toolCallId: "x",
      delta: "def",
    });
    expect(m.get("x")!.inputDelta).toBe("abcdef");
  });

  it("flips status to complete on output-available", () => {
    let m = ingestToolEvent(new Map(), {
      type: "tool-input-start",
      toolCallId: "x",
      tool: "t",
    });
    m = ingestToolEvent(m, {
      type: "tool-output-available",
      toolCallId: "x",
      output: { ok: true },
    });
    expect(m.get("x")!.status).toBe("complete");
  });

  it("flips status to error on isError output", () => {
    let m = ingestToolEvent(new Map(), {
      type: "tool-input-start",
      toolCallId: "x",
      tool: "t",
    });
    m = ingestToolEvent(m, {
      type: "tool-output-available",
      toolCallId: "x",
      output: { exit_code: 1 },
      isError: true,
    });
    expect(m.get("x")!.status).toBe("error");
  });

  it("creates a frame even when output arrives without a prior start", () => {
    const m = ingestToolEvent(new Map(), {
      type: "tool-output-available",
      toolCallId: "orphan",
      output: 42,
    });
    expect(m.get("orphan")).toBeDefined();
    expect(m.get("orphan")!.status).toBe("complete");
  });

  it("tool-error sets status=error with message", () => {
    let m = ingestToolEvent(new Map(), {
      type: "tool-input-start",
      toolCallId: "x",
      tool: "t",
    });
    m = ingestToolEvent(m, {
      type: "tool-error",
      toolCallId: "x",
      message: "oops",
    });
    expect(m.get("x")!.status).toBe("error");
    expect(m.get("x")!.errorMessage).toBe("oops");
  });
});
