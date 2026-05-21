import { type Component, createSignal, Show } from "solid-js";
import { useNavigate } from "@solidjs/router";
import { auth } from "../lib/auth";
import { Button, Input } from "../components/ui";

interface PydanticValidationError {
  type: string;
  loc: string[];
  msg: string;
  ctx?: Record<string, unknown>;
}

/** Map an ApiError / unknown thrown value to a single human-readable
 *  string.  FastAPI returns ``{detail: "string"}`` for HTTPException
 *  failures + ``{detail: [{loc, msg, ...}]}`` for Pydantic validation
 *  errors.  Both must render legibly in the form's error banner. */
function humanReadableError(err: unknown): string {
  const body = (err as { body?: unknown } | null)?.body;
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d.length > 0) {
      // Array ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ take the first validation error and format as
      // "field: message".
      const first = d[0] as Partial<PydanticValidationError>;
      const field = Array.isArray(first.loc)
        ? first.loc.filter((s) => s !== "body").join(".")
        : "field";
      return `${field}: ${first.msg ?? "invalid"}`;
    }
  }
  if (err instanceof Error) return err.message;
  return "Sign-in failed";
}

/**
 * Login + register form.  Switches between modes via a toggle.
 * On success the auth store updates + we redirect to "/".
 */
export const Login: Component = () => {
  const nav = useNavigate();
  const [mode, setMode] = createSignal<"login" | "register">("login");
  const [identifier, setIdentifier] = createSignal("");
  const [email, setEmail] = createSignal("");
  const [password, setPassword] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);

  const submit = async (e: Event) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode() === "login") {
        await auth.login(identifier(), password());
      } else {
        await auth.register(identifier(), password(), email());
      }
      nav("/", { replace: true });
    } catch (err: unknown) {
      setError(humanReadableError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      data-mode="system"
      class="flex min-h-screen items-center justify-center bg-bg-canvas px-4 text-text-display"
    >
      <form
        onSubmit={submit}
        class="w-full max-w-sm space-y-4 rounded-lg border border-border-subtle bg-bg-elevated p-6 shadow-md"
        aria-label={mode() === "login" ? "Sign in" : "Create account"}
      >
        <header class="space-y-1">
          <h1 class="text-xl font-semibold tracking-tight">
            {mode() === "login" ? "Sign in to AMOR" : "Create your account"}
          </h1>
          <p class="text-sm text-text-body">
            Local-first distributed AI desktop.
          </p>
        </header>

        <label class="block space-y-1.5">
          <span class="text-xs font-medium text-text-body">
            {mode() === "login" ? "Username or email" : "Username"}
          </span>
          <Input
            value={identifier()}
            onInput={(e) => setIdentifier(e.currentTarget.value)}
            autocomplete={mode() === "login" ? "username" : "username"}
            required
            placeholder={mode() === "login" ? "you@example.com" : "ada"}
          />
        </label>

        <Show when={mode() === "register"}>
          <label class="block space-y-1.5">
            <span class="text-xs font-medium text-text-body">Email</span>
            <Input
              type="email"
              value={email()}
              onInput={(e) => setEmail(e.currentTarget.value)}
              autocomplete="email"
              required
              placeholder="ada@example.com"
            />
          </label>
        </Show>

        <label class="block space-y-1.5">
          <span class="text-xs font-medium text-text-body">Password</span>
          <Input
            type="password"
            value={password()}
            onInput={(e) => setPassword(e.currentTarget.value)}
            autocomplete={mode() === "login" ? "current-password" : "new-password"}
            required
            minlength={mode() === "register" ? 10 : 1}
          />
          <Show when={mode() === "register"}>
            <p class="-mt-1 text-[0.65rem] text-text-subtle">
              At least 10 chars ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· upper + lower + 1 digit.
            </p>
          </Show>
        </label>

        <Show when={error()}>
          <div
            role="alert"
            class="rounded-md border px-3 py-2 text-sm"
            style={{
              "border-color": "var(--color-status-failed)",
              color: "var(--color-status-failed)",
            }}
          >
            {error()}
          </div>
        </Show>

        <Button type="submit" loading={busy()} class="w-full">
          {mode() === "login" ? "Sign in" : "Create account"}
        </Button>

        <div class="text-center text-xs text-text-subtle">
          {mode() === "login" ? (
            <>
              No account?{" "}
              <button
                type="button"
                class="text-text-display underline-offset-2 hover:underline"
                onClick={() => setMode("register")}
              >
                Create one
              </button>
            </>
          ) : (
            <>
              Already registered?{" "}
              <button
                type="button"
                class="text-text-display underline-offset-2 hover:underline"
                onClick={() => setMode("login")}
              >
                Sign in
              </button>
            </>
          )}
        </div>
      </form>
    </div>
  );
};
