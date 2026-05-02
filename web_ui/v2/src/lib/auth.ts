/**
 * Auth store — SolidJS signals over the in-memory access token + the
 * minimal user profile.  Wires the ``api`` module's
 * ``setAccessToken``/``onAuthChange`` so the typed client always
 * sees the latest token.
 *
 * The refresh token lives in an httponly cookie that the browser
 * sends automatically; we never read or write it from JS.
 *
 * Login / register flows return ``{ access_token, user }``; we
 * stash the token via ``setAccessToken`` and the user via
 * ``setUser``.  Logout clears both + hits ``/api/auth/logout``.
 */

import { createSignal } from "solid-js";
import {
  api,
  onAuthChange,
  setAccessToken,
  type ApiError,
} from "./api";
import { resetAllChatStreams } from "./chat-stream";

export interface User {
  id: string;
  username: string;
  email?: string;
  display_name?: string;
  created_at?: string;
}

interface AuthTokens {
  access_token: string;
  expires_in?: number;
  user: User;
}

const [user, setUserSignal] = createSignal<User | null>(null);
const [token, setTokenSignal] = createSignal<string | null>(null);
const [bootstrapped, setBootstrapped] = createSignal<boolean>(false);

/** When the api module clears the in-memory access token (refresh
 *  failure, explicit logout), cascade-clear the auth store + every
 *  cached chat stream + Build's module-scoped state.  The route
 *  guard's ``<Show when={auth.user()}>`` then bounces the user back
 *  to /login automatically.  Lazy import to dodge the cycle:
 *  Build → chat-stream → auth → Build. */
onAuthChange((newToken) => {
  setTokenSignal(newToken);
  if (newToken === null) {
    setUserSignal(null);
    resetAllChatStreams();
    import("../routes/Build")
      .then((m) => m.resetBuild?.())
      .catch(() => {
        /* ignore — Build chunk not loaded yet */
      });
  }
});

export const auth = {
  user,
  token,
  bootstrapped,

  /** Try to silently re-establish the session by hitting refresh.
   *  Called once at app boot.  Sets ``bootstrapped(true)`` either
   *  way so the UI knows to show login vs the chat surface. */
  async bootstrap(): Promise<void> {
    try {
      // ``setAccessToken`` runs inside refresh on success.
      const me = await api.get<User>("/api/auth/me");
      setUserSignal(me);
    } catch {
      // No active session — that's fine, user can log in.
    } finally {
      setBootstrapped(true);
    }
  },

  async login(identifier: string, password: string): Promise<User> {
    const tokens = await api.post<AuthTokens>("/api/auth/login", {
      identifier,
      password,
    });
    setAccessToken(tokens.access_token);
    setTokenSignal(tokens.access_token);
    setUserSignal(tokens.user);
    return tokens.user;
  },

  async register(
    username: string,
    password: string,
    email: string,
    displayName?: string,
  ): Promise<User> {
    const tokens = await api.post<AuthTokens>("/api/auth/register", {
      username,
      password,
      email,
      display_name: displayName,
    });
    setAccessToken(tokens.access_token);
    setTokenSignal(tokens.access_token);
    setUserSignal(tokens.user);
    return tokens.user;
  },

  async logout(): Promise<void> {
    try {
      await api.post("/api/auth/logout");
    } catch {
      // Best-effort — client clears local state regardless.
    }
    // ``setAccessToken(null)`` triggers the ``onAuthChange``
    // subscriber above which clears user + chat caches + Build
    // state in one place.  Keeping the cleanup centralised means
    // an api-driven logout (refresh-after-401 fails) follows the
    // same code path as a button-driven logout.
    setAccessToken(null);
  },
};

export type { ApiError };
