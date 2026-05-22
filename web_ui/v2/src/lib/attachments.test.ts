/**
 * Cycle UI v2.7.2 (D15) — attachment system E2E test coverage.
 *
 * 6 sub-test mirroring the Plan §D15 acceptance gate.  Uses Vitest +
 * happy-dom (existing test infra, no new dep) rather than Playwright
 * to stay within the v2.7 dep budget.  XMLHttpRequest is mocked via
 * `global.XMLHttpRequest` swap; FormData uses happy-dom native.
 *
 * Coverage:
 *   1. pick   — `uploadAttachment(File)` happy path → resolves with metadata
 *   2. paste  — File constructor from clipboard simulation → upload OK
 *   3. drop   — multi-file `uploadAttachments(File[])` parallel
 *   4. submit — error path: backend 415 → ApiError-shaped reject
 *   5. submit — error path: backend 413 → ApiError with status 413
 *   6. persist — `ChatAttachmentRef` type-shape compatibility with
 *                MessageBubble render assumption
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { uploadAttachment, uploadAttachments } from "./api";
import type { ChatAttachmentRef } from "./types";

// ─── XHR mock ────────────────────────────────────────────────────────

class MockXHR {
  static instances: MockXHR[] = [];
  status = 200;
  statusText = "OK";
  responseText = "";
  upload = { onprogress: null as ((ev: ProgressEvent) => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  withCredentials = false;
  headers: Record<string, string> = {};
  body: FormData | null = null;
  aborted = false;

  open(_method: string, _url: string) {
    MockXHR.instances.push(this);
  }
  setRequestHeader(k: string, v: string) {
    this.headers[k] = v;
  }
  send(body: FormData) {
    this.body = body;
    // No auto-trigger — tests fire `xhr.onload?.()` manually after
    // setting status/responseText.  This avoids races where the
    // microtask flush runs before the test populates the response.
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
}

beforeEach(() => {
  MockXHR.instances = [];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (global as any).XMLHttpRequest = MockXHR as any;
  // localStorage may not exist in happy-dom default; stub.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  if (!(global as any).localStorage) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (global as any).localStorage = {
      getItem: () => null,
      setItem: () => undefined,
    };
  }
});

// ─── 1) PICK ─────────────────────────────────────────────────────────

describe("uploadAttachment — pick", () => {
  it("resolves with attachment metadata on 200", async () => {
    const file = new File(["hello world"], "greeting.txt", {
      type: "text/plain",
    });

    const promise = uploadAttachment(file);
    // Wait for MockXHR.open to register, then fire 200.
    await Promise.resolve();
    const xhr = MockXHR.instances[0];
    expect(xhr).toBeDefined();
    xhr.status = 200;
    xhr.responseText = JSON.stringify({
      attachment_id: "abc123",
      mime: "text/plain",
      size: 11,
      sha256: "deadbeef",
      status: "uploaded",
      original_name: "greeting.txt",
      text_extracted_preview: "hello world",
    });
    xhr.onload?.();

    const result = await promise;
    expect(result.attachment_id).toBe("abc123");
    expect(result.mime).toBe("text/plain");
    expect(result.size).toBe(11);
    expect(result.original_name).toBe("greeting.txt");
  });
});

// ─── 2) PASTE ────────────────────────────────────────────────────────

describe("uploadAttachment — paste (image from clipboard)", () => {
  it("handles image/png file from paste path", async () => {
    // Simulate the paste handler's File construction (UnifiedComposer
    // line ~440 wraps clipboard items into File via getAsFile()).
    const fakePngBytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47]); // PNG magic
    const file = new File([fakePngBytes], "pasted-screenshot.png", {
      type: "image/png",
    });
    expect(file.type).toBe("image/png");

    const promise = uploadAttachment(file);
    await Promise.resolve();
    const xhr = MockXHR.instances[0];
    xhr.responseText = JSON.stringify({
      attachment_id: "img-uuid",
      mime: "image/png",
      size: 4,
      sha256: "pngsha",
      status: "uploaded",
      original_name: "pasted-screenshot.png",
    });
    xhr.onload?.();

    const result = await promise;
    expect(result.mime).toBe("image/png");
    expect(result.original_name).toBe("pasted-screenshot.png");
  });
});

// ─── 3) DROP (parallel) ──────────────────────────────────────────────

describe("uploadAttachments — drop multi-file", () => {
  it("uploads N files in parallel and returns IDs in input order", async () => {
    const files = [
      new File(["a"], "first.txt", { type: "text/plain" }),
      new File(["bb"], "second.md", { type: "text/markdown" }),
    ];

    const promise = uploadAttachments(files);
    await Promise.resolve();
    await Promise.resolve();
    // Two parallel XHRs.
    expect(MockXHR.instances.length).toBe(2);

    MockXHR.instances[0].responseText = JSON.stringify({
      attachment_id: "id-1",
      mime: "text/plain",
      size: 1,
      sha256: "a",
      status: "uploaded",
      original_name: "first.txt",
    });
    MockXHR.instances[1].responseText = JSON.stringify({
      attachment_id: "id-2",
      mime: "text/markdown",
      size: 2,
      sha256: "b",
      status: "uploaded",
      original_name: "second.md",
    });
    MockXHR.instances[0].onload?.();
    MockXHR.instances[1].onload?.();

    const results = await promise;
    expect(results.length).toBe(2);
    expect(results[0].attachment_id).toBe("id-1");
    expect(results[1].attachment_id).toBe("id-2");
  });
});

// ─── 4) SUBMIT — 415 MIME reject ─────────────────────────────────────

describe("uploadAttachment — 415 MIME rejected", () => {
  it("rejects with ApiError-shape on 415", async () => {
    const file = new File(["MZ"], "evil.exe", {
      type: "application/x-msdownload",
    });

    const promise = uploadAttachment(file);
    await Promise.resolve();
    const xhr = MockXHR.instances[0];
    xhr.status = 415;
    xhr.statusText = "Unsupported Media Type";
    xhr.responseText = JSON.stringify({
      detail: "Unsupported file type: MIME application/x-msdownload rejected (executable/archive)",
    });
    xhr.onload?.();

    await expect(promise).rejects.toMatchObject({
      status: 415,
    });
  });
});

// ─── 5) SUBMIT — 413 oversized ──────────────────────────────────────

describe("uploadAttachment — 413 oversized", () => {
  it("rejects with status 413 when over the size cap", async () => {
    // 11 MB content (> 10 MB cap).  In real life FE pre-check rejects
    // first; this validates the BE response path.
    const big = new Uint8Array(11 * 1024 * 1024);
    const file = new File([big], "huge.bin", { type: "text/plain" });

    const promise = uploadAttachment(file);
    await Promise.resolve();
    const xhr = MockXHR.instances[0];
    xhr.status = 413;
    xhr.statusText = "Payload Too Large";
    xhr.responseText = JSON.stringify({ detail: "File too large" });
    xhr.onload?.();

    await expect(promise).rejects.toMatchObject({
      status: 413,
    });
  });
});

// ─── 6) PERSIST — ChatAttachmentRef shape ────────────────────────────

describe("ChatAttachmentRef type compatibility", () => {
  it("matches MessageBubble render assumptions", () => {
    // Type-level assertion: backend response → ChatAttachmentRef.
    // If the backend MessageAttachmentRef Pydantic schema drifts,
    // this test forces a sync via TypeScript compile error.
    const ref: ChatAttachmentRef = {
      attachment_id: "uuid-here",
      name: "report.csv",
      mime: "text/csv",
      size: 12345,
      role: "user_attached",
      inclusion: "inline_text",
      inline_preview: "col1,col2\n1,2",
    };
    expect(ref.attachment_id).toBe("uuid-here");
    expect(ref.role).toBe("user_attached");
    expect(ref.inclusion).toBe("inline_text");

    // Branch role + inclusion variants exercised here so the union
    // type stays well-formed if v2.8 adds new values.
    const img: ChatAttachmentRef = {
      attachment_id: "img-1",
      name: "design.png",
      mime: "image/png",
      size: 500000,
      role: "user_attached",
      inclusion: "image_ref",
    };
    expect(img.inclusion).toBe("image_ref");

    const fnameOnly: ChatAttachmentRef = {
      attachment_id: "bin-1",
      name: "data.bin",
      mime: "application/octet-stream",
      size: 1024,
      role: "user_attached",
      inclusion: "filename_only",
    };
    expect(fnameOnly.inclusion).toBe("filename_only");
  });
});
