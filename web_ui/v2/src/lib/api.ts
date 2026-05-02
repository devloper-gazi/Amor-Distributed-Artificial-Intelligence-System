/**
 * AMOR API client.
 *
 * Single-shot fetch wrapper with three responsibilities:
 *
 *  1. Attach the current ``Authorization: Bearer <access_token>`` to
 *     every request when the auth store has a token.
 *  2. On a 401, call ``POST /api/auth/refresh`` once.  The refresh
 *     token lives in an httponly cookie set during /login or
 *     /register so the browser sends it automatically; we read the
 *     new ``access_token`` out of the JSON body and replay the
 *     original request EXACTLY ONCE.
 *  3. Map non-2xx responses to typed exceptions
 *     (``ApiError``, ``AuthError``) with status + parsed body so
 *     callers can match on shape.
 *
 * The client is intentionally framework-agnostic — it doesn't touch
 * SolidJS signals.  ``setAccessToken`` is exposed so the auth store
 * (``src/lib/auth.ts``) can wire its setter at app boot.
 *
 * Usage:
 *
 *   import { api } from "@/lib/api";
 *   const { user } = await api.get<{ user: User }>("/api/auth/me");
 *   const session = await api.post<SessionResp>("/api/code/start", {
 *     prompt: "...", effort: "medium",
 *   });
 */

let _accessToken: string | null = null;
let _onAuthChange: ((token: string | null) => void) | null = null;

/** Set the in-memory access token + notify subscribers. */
export function setAccessToken(token: string | null): void {
  _accessToken = token;
  if (_onAuthChange) _onAuthChange(token);
}

/** Read the current access token (used by ``EventSource`` setups
 *  that can't add headers — they pass the token via the cookie
 *  instead). */
export function getAccessToken(): string | null {
  return _accessToken;
}

/** Stable per-browser client id used as the ``X-Client-Id`` header
 *  every consortium / sentinel / chat-session API call requires.
 *  Persisted in localStorage so the same id flows across reloads.
 */
export function getClientId(): string {
  try {
    let v = localStorage.getItem("amor.client_id");
    if (!v) {
      v =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `cli-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      localStorage.setItem("amor.client_id", v);
    }
    return v;
  } catch {
    return `cli-${Date.now().toString(36)}`;
  }
}

/** Subscribe to access-token changes.  Returns the unsubscribe
 *  function.  Used by the auth store + by the SSE wrapper to
 *  re-establish the EventSource when the token rotates. */
export function onAuthChange(
  cb: (token: string | null) => void,
): () => void {
  _onAuthChange = cb;
  return () => {
    if (_onAuthChange === cb) _onAuthChange = null;
  };
}

/** Generic API failure. */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `API error: ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/** A 401 that survived the refresh-and-replay attempt — caller
 *  must treat the user as logged out. */
export class AuthError extends ApiError {
  constructor(body: unknown) {
    super(401, body, "Unauthorized");
    this.name = "AuthError";
  }
}

interface RefreshBody {
  access_token: string;
  expires_in?: number;
  user?: unknown;
}

/** Hit /api/auth/refresh.  Returns the new access_token or throws.
 *  On a hard 401 (no valid refresh cookie), broadcast the logout
 *  via ``setAccessToken(null)`` so subscribers (auth store, SSE
 *  wrapper, route guard) can cascade-clear their state instead of
 *  leaving the UI in a half-authenticated zombie. */
async function refreshAccessToken(): Promise<string> {
  const resp = await fetch("/api/auth/refresh", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  });
  if (!resp.ok) {
    let body: unknown = null;
    try {
      body = await resp.json();
    } catch {
      // ignore
    }
    setAccessToken(null);
    throw new AuthError(body);
  }
  const json = (await resp.json()) as RefreshBody;
  if (!json.access_token) {
    setAccessToken(null);
    throw new AuthError(json);
  }
  setAccessToken(json.access_token);
  return json.access_token;
}

interface FetchOptions extends Omit<RequestInit, "headers" | "body"> {
  /** JSON-serialised request body.  Use ``raw`` for non-JSON shapes. */
  json?: unknown;
  raw?: RequestInit["body"];
  headers?: Record<string, string>;
  /** Override the default 30 s timeout (ms).  ``null`` disables. */
  timeoutMs?: number | null;
}

const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * One-shot wrapper.  Resolves to the parsed JSON body (typed as
 * ``T``) on 2xx; throws ``ApiError`` / ``AuthError`` otherwise.
 */
async function call<T>(
  path: string,
  init: FetchOptions & { method?: string },
  /** Internal — set when this call is the replay after a refresh.
   *  Prevents an infinite loop if the second 401 is genuine. */
  isReplay: boolean = false,
): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = init.timeoutMs;
  const timer =
    timeoutMs === null
      ? null
      : setTimeout(
          () => controller.abort(new Error("timeout")),
          timeoutMs ?? DEFAULT_TIMEOUT_MS,
        );

  const headers: Record<string, string> = { ...(init.headers ?? {}) };
  if (init.json !== undefined && headers["Content-Type"] === undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (_accessToken) {
    headers["Authorization"] = `Bearer ${_accessToken}`;
  }
  // X-Client-Id is required by consortium + sentinel + chat-session
  // endpoints and accepted (ignored) by every other route.  Always
  // include it unless the caller explicitly set it.
  if (headers["X-Client-Id"] === undefined) {
    headers["X-Client-Id"] = getClientId();
  }

  let resp: Response;
  try {
    resp = await fetch(path, {
      ...init,
      method: init.method ?? "GET",
      headers,
      body:
        init.json !== undefined ? JSON.stringify(init.json) : init.raw,
      credentials: "include",
      signal: controller.signal,
    });
  } finally {
    if (timer) clearTimeout(timer);
  }

  if (resp.status === 401 && !isReplay) {
    // Refresh + replay exactly once.
    try {
      await refreshAccessToken();
    } catch (e) {
      // Refresh failed — propagate as AuthError so callers can
      // bounce the user to /login.
      throw e;
    }
    return call<T>(path, init, /* isReplay */ true);
  }

  if (!resp.ok) {
    let body: unknown = null;
    try {
      body = await resp.json();
    } catch {
      try {
        body = await resp.text();
      } catch {
        // ignore
      }
    }
    if (resp.status === 401) {
      throw new AuthError(body);
    }
    throw new ApiError(resp.status, body);
  }

  // 204 No Content — return null cast as T.
  if (resp.status === 204) {
    return null as T;
  }
  // Best-effort JSON parse.  If callers want raw text they can use
  // ``api.fetchRaw`` (added when a use case appears).
  const ct = resp.headers.get("Content-Type") ?? "";
  if (ct.includes("application/json")) {
    return (await resp.json()) as T;
  }
  return (await resp.text()) as unknown as T;
}

export const api = {
  get<T>(path: string, init: Omit<FetchOptions, "method" | "json"> = {}) {
    return call<T>(path, { ...init, method: "GET" });
  },
  post<T>(path: string, json?: unknown, init: Omit<FetchOptions, "method"> = {}) {
    return call<T>(path, { ...init, method: "POST", json });
  },
  put<T>(path: string, json?: unknown, init: Omit<FetchOptions, "method"> = {}) {
    return call<T>(path, { ...init, method: "PUT", json });
  },
  patch<T>(path: string, json?: unknown, init: Omit<FetchOptions, "method"> = {}) {
    return call<T>(path, { ...init, method: "PATCH", json });
  },
  del<T>(path: string, init: Omit<FetchOptions, "method" | "json"> = {}) {
    return call<T>(path, { ...init, method: "DELETE" });
  },
};

export type { FetchOptions };
