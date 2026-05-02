import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  api,
  ApiError,
  AuthError,
  setAccessToken,
  getAccessToken,
} from "./api";

/** Build a Response stub that ``fetch`` mocks return. */
function jsonResp(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function emptyResp(status: number): Response {
  return new Response(null, { status });
}

describe("api client", () => {
  beforeEach(() => {
    setAccessToken(null);
    vi.restoreAllMocks();
  });

  it("returns parsed JSON on 2xx", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResp(200, { hello: "world" }),
    );
    const got = await api.get<{ hello: string }>("/api/x");
    expect(got).toEqual({ hello: "world" });
  });

  it("attaches Authorization: Bearer when a token is set", async () => {
    setAccessToken("tok-123");
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResp(200, { ok: true }));
    await api.get("/api/x");
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(init.headers).toMatchObject({
      Authorization: "Bearer tok-123",
    });
  });

  it("does NOT attach Authorization when no token is set", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResp(200, { ok: true }));
    await api.get("/api/x");
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)["Authorization"]).toBeUndefined();
  });

  it("posts JSON body with the right Content-Type", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResp(200, { id: 1 }));
    await api.post("/api/x", { name: "ada" });
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
    expect(init.body).toBe(JSON.stringify({ name: "ada" }));
  });

  it("throws ApiError on 4xx with body shape", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResp(400, { detail: "bad" }),
    );
    await expect(api.get("/api/x")).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      body: { detail: "bad" },
    });
  });

  it("throws ApiError on 5xx", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResp(500, { detail: "boom" }),
    );
    await expect(api.get("/api/x")).rejects.toBeInstanceOf(ApiError);
  });

  it("returns null on 204 No Content", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(emptyResp(204));
    const got = await api.del("/api/x");
    expect(got).toBeNull();
  });

  it("on 401 calls /api/auth/refresh once and replays the original", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      // first call: original request → 401
      .mockResolvedValueOnce(jsonResp(401, { detail: "token expired" }))
      // second call: refresh → 200 with new token
      .mockResolvedValueOnce(
        jsonResp(200, {
          access_token: "tok-new",
          expires_in: 900,
          user: { id: "u" },
        }),
      )
      // third call: replay → 200
      .mockResolvedValueOnce(jsonResp(200, { ok: true }));

    const got = await api.get<{ ok: boolean }>("/api/protected");
    expect(got).toEqual({ ok: true });
    expect(fetchSpy).toHaveBeenCalledTimes(3);

    // Refresh URL is the second call.
    const refreshUrl = fetchSpy.mock.calls[1]?.[0];
    expect(refreshUrl).toBe("/api/auth/refresh");

    // Replay carries the rotated Bearer token.
    const replayInit = fetchSpy.mock.calls[2]?.[1] as RequestInit;
    expect(replayInit.headers).toMatchObject({
      Authorization: "Bearer tok-new",
    });

    // In-memory token reflects the rotation.
    expect(getAccessToken()).toBe("tok-new");
  });

  it("does NOT replay a second time if the replay also 401s", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResp(401, { detail: "expired" }))
      .mockResolvedValueOnce(
        jsonResp(200, { access_token: "tok-new", user: { id: "u" } }),
      )
      .mockResolvedValueOnce(jsonResp(401, { detail: "still bad" }));

    await expect(api.get("/api/x")).rejects.toBeInstanceOf(AuthError);
    // Exactly 3 calls — the second 401 does NOT trigger another
    // refresh+replay loop.
    expect(fetchSpy).toHaveBeenCalledTimes(3);
  });

  it("throws AuthError when refresh itself returns 401", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResp(401, { detail: "expired" }))
      .mockResolvedValueOnce(jsonResp(401, { detail: "no refresh cookie" }));

    await expect(api.get("/api/x")).rejects.toBeInstanceOf(AuthError);
  });

  it("respects credentials: include for cookies", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResp(200, {}));
    await api.get("/api/x");
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(init.credentials).toBe("include");
  });
});
