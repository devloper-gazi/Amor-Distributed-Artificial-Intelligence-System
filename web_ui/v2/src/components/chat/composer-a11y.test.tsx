/**
 * Cycle C Sprint 4 Day 5 — axe-core a11y gate.
 *
 * Mounts the new Sprint 4 surfaces (UnifiedComposer, MessageActions,
 * ToolCallCard) under happy-dom + axe-core and asserts zero
 * violations.  Two intentional limitations:
 *
 *   * happy-dom doesn't compute layout, so axe's *contrast* rule
 *     can't be evaluated meaningfully.  We disable it here; a
 *     follow-up Playwright-axe job in CI can re-enable it against
 *     a real browser.
 *   * axe needs a DOM root — we render into ``<div id="root"/>``
 *     mounted on the jsdom-style ``document``.
 *
 * Pinned rules (must stay green):
 *   * button-name, label, aria-allowed-attr, aria-required-attr,
 *     aria-valid-attr, aria-roles, role-img-alt, focus-order-semantics,
 *     listbox children, no duplicate-id-aria.
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@solidjs/testing-library";
import axe, { type AxeResults, type Result } from "axe-core";

import { UnifiedComposer } from "./UnifiedComposer";
import { MessageActions } from "./MessageActions";
import { ToolCallCard } from "./ToolCallCard";
import { BranchNavigator } from "./BranchNavigator";
import type { ToolCallFrame } from "../../lib/tool-stream";
import type { ChatTurn } from "../../lib/types";

// Disable rules that don't work in happy-dom (no real layout / colours).
const DISABLED_RULES: string[] = [
  "color-contrast",
  "color-contrast-enhanced",
  "region",        // region wrapper enforced at app shell, not unit
  "landmark-one-main",
  "page-has-heading-one",
];

async function runAxe(node: Element): Promise<AxeResults> {
  return axe.run(node, {
    rules: Object.fromEntries(
      DISABLED_RULES.map((id) => [id, { enabled: false }]),
    ),
    resultTypes: ["violations"],
  });
}

function describeViolations(violations: Result[]): string {
  if (violations.length === 0) return "no violations";
  return violations
    .map((v) => {
      const targets = v.nodes
        .map((n) => n.target.join(", "))
        .slice(0, 3)
        .join(" / ");
      return `${v.id} (${v.impact}): ${v.help} — ${targets}`;
    })
    .join("\n");
}

afterEach(() => cleanup());

describe("UnifiedComposer a11y", () => {
  it("has zero axe violations on the closed-picker surface", async () => {
    const { container } = render(() => (
      <UnifiedComposer onSubmit={() => {}} />
    ));
    const results = await runAxe(container);
    expect(
      results.violations,
      describeViolations(results.violations),
    ).toEqual([]);
  });
});

describe("MessageActions a11y", () => {
  it("toolbar passes axe with both rate buttons rendered", async () => {
    const turn: ChatTurn = {
      id: "t-1",
      role: "assistant",
      content: "ok",
    };
    const { container } = render(() => (
      <MessageActions
        turn={turn}
        onRegenerate={() => {}}
        onBranch={() => {}}
        onRate={() => {}}
      />
    ));
    const results = await runAxe(container);
    expect(
      results.violations,
      describeViolations(results.violations),
    ).toEqual([]);
  });

  it("user-turn variant (edit, no rate) passes axe", async () => {
    const turn: ChatTurn = {
      id: "t-2",
      role: "user",
      content: "hello",
    };
    const { container } = render(() => (
      <MessageActions turn={turn} onEdit={() => {}} />
    ));
    const results = await runAxe(container);
    expect(
      results.violations,
      describeViolations(results.violations),
    ).toEqual([]);
  });
});

describe("ToolCallCard a11y", () => {
  const baseFrame: ToolCallFrame = {
    id: "call-1",
    tool: "sandbox-execute",
    status: "complete",
    inputDelta: "",
    input: { language: "python" },
    output: { exit_code: 0, stdout: "ok", stderr: "" },
    isError: false,
  };

  it("complete state passes axe", async () => {
    const { container } = render(() => <ToolCallCard frame={baseFrame} />);
    const results = await runAxe(container);
    expect(
      results.violations,
      describeViolations(results.violations),
    ).toEqual([]);
  });

  it("error state passes axe", async () => {
    const errFrame: ToolCallFrame = {
      ...baseFrame,
      status: "error",
      isError: true,
      output: { exit_code: 1, stdout: "", stderr: "boom" },
    };
    const { container } = render(() => <ToolCallCard frame={errFrame} />);
    const results = await runAxe(container);
    expect(
      results.violations,
      describeViolations(results.violations),
    ).toEqual([]);
  });

  it("running state with delta payload passes axe", async () => {
    const runningFrame: ToolCallFrame = {
      ...baseFrame,
      status: "running",
      input: undefined,
      output: undefined,
      inputDelta: '{"packages":["numpy"]}',
    };
    const { container } = render(() => (
      <ToolCallCard frame={runningFrame} />
    ));
    const results = await runAxe(container);
    expect(
      results.violations,
      describeViolations(results.violations),
    ).toEqual([]);
  });
});


// Cycle UI Phase 4 — BranchNavigator a11y coverage
describe("BranchNavigator a11y", () => {
  it("hidden when total <= 1 (renders nothing → axe-clean by default)", async () => {
    const { container } = render(() => (
      <BranchNavigator current={1} total={1} onSelect={() => {}} />
    ));
    const results = await runAxe(container);
    expect(
      results.violations,
      describeViolations(results.violations),
    ).toEqual([]);
    // Sanity: the role="group" wrapper must NOT have been rendered.
    expect(container.querySelector("[data-amor-branch-nav]")).toBeNull();
  });
  it("multi-branch navigator passes axe with both arrows + counter", async () => {
    const { container } = render(() => (
      <BranchNavigator current={2} total={4} onSelect={() => {}} />
    ));
    const results = await runAxe(container);
    expect(
      results.violations,
      describeViolations(results.violations),
    ).toEqual([]);
    // Sanity: aria-live counter present + both arrows have aria-label.
    const counter = container.querySelector("[data-amor-branch-counter]");
    expect(counter?.getAttribute("aria-live")).toBe("polite");
    const prev = container.querySelector("[data-amor-branch-prev]");
    const next = container.querySelector("[data-amor-branch-next]");
    expect(prev?.getAttribute("aria-label")).toBeTruthy();
    expect(next?.getAttribute("aria-label")).toBeTruthy();
  });
  it("disabled state when busy=true", async () => {
    const { container } = render(() => (
      <BranchNavigator current={1} total={3} onSelect={() => {}} busy />
    ));
    const results = await runAxe(container);
    expect(
      results.violations,
      describeViolations(results.violations),
    ).toEqual([]);
    const prev = container.querySelector("[data-amor-branch-prev]");
    expect((prev as HTMLButtonElement | null)?.disabled).toBe(true);
  });
});
