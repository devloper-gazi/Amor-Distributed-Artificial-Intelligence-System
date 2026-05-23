/**
 * Cycle F Sprint 5 — ApprovalPrompt unit + behavioural tests.
 *
 * Coverage:
 *   * Renders title + tool name + status pill ("waiting")
 *   * Approve button POSTs + transitions to "approved"
 *   * Deny button parallel path
 *   * POST failure surfaces error text + status="error"
 *   * Terminal payloads render WITHOUT buttons
 *   * i18n keys present in both en and tr tables
 *
 * Uses `getByRole("button", { name: ... })` for buttons to avoid
 * Testing Library's "found multiple elements" trap on text that
 * also appears in aria-live regions.
 */

import { cleanup, fireEvent, render, screen } from "@solidjs/testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApprovalPrompt } from "./ApprovalPrompt";
import { api } from "../../lib/api";
import { en } from "../../i18n/en";
import { tr } from "../../i18n/tr";
import type { ApprovalPayload } from "../../lib/types";


function basePayload(
  overrides: Partial<ApprovalPayload> = {},
): ApprovalPayload {
  return {
    request_id: "req-abc",
    tool_name: "rm_rf",
    category: "delete",
    arguments: { path: "/tmp/x" },
    actor_role: "coder",
    timeout_s: 90,
    status: "pending",
    ...overrides,
  };
}


// Vitest's MockInstance generic doesn't unify with api.post's generic
// arrow signature in strict mode.  Widen to `any` here — this is a
// test file only; runtime behaviour is unaffected.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let postSpy: any;


beforeEach(() => {
  postSpy = vi.spyOn(api, "post").mockResolvedValue({
    resolved: true,
    approved: true,
  } as unknown as never);
});


afterEach(() => {
  // SolidJS testing-library doesn't auto-cleanup; without this,
  // each test inherits the previous test's mounted nodes.
  cleanup();
  postSpy.mockRestore();
});


describe("ApprovalPrompt — render", () => {
  it("shows the approval card root with data-amor-approval attribute", () => {
    const { container } = render(
      () => <ApprovalPrompt payload={basePayload()} />,
    );
    const card = container.querySelector("[data-amor-approval]");
    expect(card).toBeTruthy();
    expect(card!.getAttribute("data-amor-request-id")).toBe("req-abc");
    expect(card!.getAttribute("data-amor-status")).toBe("pending");
  });

  it("renders tool_name in the header", () => {
    render(() => <ApprovalPrompt payload={basePayload()} />);
    // Tool name is the only occurrence of "rm_rf".
    expect(screen.getByText("rm_rf")).toBeTruthy();
  });

  it("renders Approve + Deny buttons in pending state", () => {
    render(() => <ApprovalPrompt payload={basePayload()} />);
    const approve = screen.getByRole("button", { name: "Approve" });
    const deny = screen.getByRole("button", { name: "Deny" });
    expect(approve).toBeTruthy();
    expect(deny).toBeTruthy();
    expect((approve as HTMLButtonElement).disabled).toBe(false);
    expect((deny as HTMLButtonElement).disabled).toBe(false);
  });
});


describe("ApprovalPrompt — Approve flow", () => {
  it("clicking Approve POSTs to /api/approval/{id} with approved=true", async () => {
    const onChange = vi.fn();
    render(() => (
      <ApprovalPrompt
        payload={basePayload()}
        onStatusChange={onChange}
      />
    ));
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await vi.waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith(
        "/api/approval/req-abc",
        { approved: true },
      );
    });
    await vi.waitFor(() => {
      expect(onChange).toHaveBeenCalledWith("approved");
    });
  });

  it("buttons removed after Approve resolves", async () => {
    render(() => <ApprovalPrompt payload={basePayload()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await vi.waitFor(() => {
      expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
    });
  });
});


describe("ApprovalPrompt — Deny flow", () => {
  it("clicking Deny POSTs with approved=false", async () => {
    const onChange = vi.fn();
    postSpy.mockResolvedValueOnce({
      resolved: true,
      approved: false,
    } as unknown as never);
    render(() => (
      <ApprovalPrompt
        payload={basePayload()}
        onStatusChange={onChange}
      />
    ));
    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    await vi.waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith(
        "/api/approval/req-abc",
        { approved: false },
      );
    });
    await vi.waitFor(() => {
      expect(onChange).toHaveBeenCalledWith("denied");
    });
  });
});


describe("ApprovalPrompt — error path", () => {
  it("surfaces POST failure as error status + visible message", async () => {
    postSpy.mockRejectedValueOnce(new Error("network down"));
    const onChange = vi.fn();
    render(() => (
      <ApprovalPrompt
        payload={basePayload()}
        onStatusChange={onChange}
      />
    ));
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await vi.waitFor(() => {
      expect(onChange).toHaveBeenCalledWith("error");
    });
    expect(screen.getByText(/network down/i)).toBeTruthy();
  });
});


describe("ApprovalPrompt — terminal payload", () => {
  it("renders without buttons when status is already approved", () => {
    const { container } = render(
      () => <ApprovalPrompt payload={basePayload({ status: "approved" })} />,
    );
    const card = container.querySelector("[data-amor-approval]");
    expect(card!.getAttribute("data-amor-status")).toBe("approved");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
  });

  it("renders without buttons when status is already denied", () => {
    const { container } = render(
      () => <ApprovalPrompt payload={basePayload({ status: "denied" })} />,
    );
    const card = container.querySelector("[data-amor-approval]");
    expect(card!.getAttribute("data-amor-status")).toBe("denied");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });
});


describe("ApprovalPrompt — i18n parity", () => {
  it("every key used by the component exists in both en and tr", () => {
    const required = [
      "approval.title",
      "approval.subtitle",
      "approval.arguments",
      "approval.approve",
      "approval.deny",
      "approval.timeout_warning",
      "approval.status.pending",
      "approval.status.approved",
      "approval.status.denied",
      "approval.status.timeout",
      "approval.status.error",
      "approval.category.read",
      "approval.category.write",
      "approval.category.delete",
      "approval.category.exec",
      "approval.category.network",
      "approval.category.git",
      "approval.category.db",
      "approval.category.docker",
      "approval.category.llm",
      "approval.category.secret",
      "approval.category.package",
      "approval.category.unclassified",
    ];
    for (const key of required) {
      expect(en[key], `en missing ${key}`).toBeTruthy();
      expect(tr[key], `tr missing ${key}`).toBeTruthy();
    }
  });
});
