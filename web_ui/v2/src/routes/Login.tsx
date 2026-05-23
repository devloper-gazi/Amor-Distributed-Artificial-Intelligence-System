/**
 * Login + Register surface (Cycle UI v2.5 Hotfix 3).
 *
 * Visual goals:
 *   * No more flat-black void — three OKLch mode-accent radial
 *     blobs softly light the canvas, mirroring the brand's
 *     six-mode palette without committing to one mode's identity.
 *   * Compact glass card (bg-elevated/85 + backdrop-blur) so the
 *     ambient light bleeds through and the form feels grounded
 *     in the canvas.
 *   * Brand wordmark above the card with a tiny 3-dot AMOR mark
 *     (research / build / thinking) — recognisable without a
 *     logo asset.
 *   * Three-pillar footer ("Veri sizde · Çevrimdışı · Açık kaynak")
 *     reinforces the local-first promise that Sign In is asking
 *     the user to trust.
 *   * Fully i18n'd against the ``auth.*`` namespace — English and
 *     Turkish parallel, with placeholders using
 *     ``you@amor.local`` (not ``example.com``) so first-time
 *     users grok this is a local-network product.
 *
 * Previous breakage fixed here:
 *   * Button primary variant used ``bg-text-primary`` — a legacy
 *     token deleted in Phase 3 token cleanup — so the Sign In
 *     button rendered with NO background.  Fix swapped to
 *     ``bg-text-display`` in components/ui/Button.tsx; this file
 *     just makes sure the layout is worthy of the now-visible
 *     button.
 */
import { type Component, createSignal, Show } from "solid-js";
import { useNavigate } from "@solidjs/router";
import { auth } from "../lib/auth";
import { Button, Input } from "../components/ui";
import { t } from "../i18n";

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
      const first = d[0] as Partial<PydanticValidationError>;
      const field = Array.isArray(first.loc)
        ? first.loc.filter((s) => s !== "body").join(".")
        : "field";
      return `${field}: ${first.msg ?? "invalid"}`;
    }
  }
  if (err instanceof Error) return err.message;
  return t("auth.error.generic");
}

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

  const isLogin = () => mode() === "login";

  return (
    <div
      class="relative flex min-h-dvh items-center justify-center overflow-hidden bg-bg-canvas px-4 py-10 text-text-display"
      style={{
        "padding-bottom": "max(2.5rem, env(safe-area-inset-bottom))",
      }}
    >
      {/* Ambient OKLch blobs — three mode-accent radial gradients
          painted onto the canvas at low opacity.  Each blob is a
          fixed-size absolute div blurred via Tailwind's blur-3xl.
          On prefers-reduced-motion, the subtle drift animation
          (motion.css amor-blob) is no-op so users sensitive to
          motion see a static composition. */}
      <div aria-hidden="true" class="pointer-events-none absolute inset-0 overflow-hidden">
        <div
          class="absolute -top-32 -left-24 h-[28rem] w-[28rem] rounded-full opacity-[0.18] blur-3xl motion-safe:animate-[amor-blob_18s_ease-in-out_infinite_alternate]"
          style={{ background: "var(--color-mode-research)" }}
        />
        <div
          class="absolute top-1/3 -right-32 h-[32rem] w-[32rem] rounded-full opacity-[0.16] blur-3xl motion-safe:animate-[amor-blob_22s_ease-in-out_infinite_alternate-reverse]"
          style={{ background: "var(--color-mode-build)" }}
        />
        <div
          class="absolute -bottom-40 left-1/4 h-[30rem] w-[30rem] rounded-full opacity-[0.14] blur-3xl motion-safe:animate-[amor-blob_26s_ease-in-out_infinite_alternate]"
          style={{ background: "var(--color-mode-thinking)" }}
        />
      </div>

      {/* Content column */}
      <div class="relative z-10 w-full max-w-sm">
        {/* Wordmark */}
        <header class="mb-8 flex flex-col items-center gap-3">
          <div class="flex items-center gap-2">
            <span
              class="inline-block h-2 w-2 rounded-full"
              style={{ background: "var(--color-mode-research)" }}
              aria-hidden="true"
            />
            <span
              class="inline-block h-2 w-2 rounded-full"
              style={{ background: "var(--color-mode-build)" }}
              aria-hidden="true"
            />
            <span
              class="inline-block h-2 w-2 rounded-full"
              style={{ background: "var(--color-mode-thinking)" }}
              aria-hidden="true"
            />
          </div>
          <h1 class="text-2xl font-semibold tracking-tight text-text-display">
            AMOR
          </h1>
          <p class="text-center text-sm leading-snug text-text-subtle">
            {isLogin() ? t("auth.signin.subtitle") : t("auth.register.subtitle")}
          </p>
        </header>

        {/* Glass card */}
        <form
          onSubmit={submit}
          class={[
            "w-full space-y-4 rounded-2xl border border-border-subtle",
            "bg-bg-elevated/85 p-7 shadow-xl shadow-black/30",
            "backdrop-blur-xl",
          ].join(" ")}
          aria-label={isLogin() ? t("auth.signin.title") : t("auth.register.title")}
        >
          <div>
            <h2 class="text-lg font-semibold tracking-tight text-text-display">
              {isLogin() ? t("auth.signin.title") : t("auth.register.title")}
            </h2>
          </div>

          <label class="block space-y-1.5">
            <span class="text-xs font-medium text-text-body">
              {isLogin() ? t("auth.field.identifier") : t("auth.field.username")}
            </span>
            <Input
              value={identifier()}
              onInput={(e) => setIdentifier(e.currentTarget.value)}
              autocomplete="username"
              required
              placeholder={isLogin()
                ? t("auth.placeholder.identifier")
                : t("auth.placeholder.username")}
            />
          </label>

          <Show when={!isLogin()}>
            <label class="block space-y-1.5">
              <span class="text-xs font-medium text-text-body">
                {t("auth.field.email")}
              </span>
              <Input
                type="email"
                value={email()}
                onInput={(e) => setEmail(e.currentTarget.value)}
                autocomplete="email"
                required
                placeholder={t("auth.placeholder.email")}
              />
            </label>
          </Show>

          <label class="block space-y-1.5">
            <span class="text-xs font-medium text-text-body">
              {t("auth.field.password")}
            </span>
            <Input
              type="password"
              value={password()}
              onInput={(e) => setPassword(e.currentTarget.value)}
              autocomplete={isLogin() ? "current-password" : "new-password"}
              required
              minlength={isLogin() ? 1 : 10}
            />
            <Show when={!isLogin()}>
              <p class="-mt-1 text-[0.65rem] text-text-subtle">
                {t("auth.password.hint")}
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
            {isLogin() ? t("auth.cta.signin") : t("auth.cta.register")}
          </Button>

          <div class="text-center text-xs text-text-subtle">
            <Show
              when={isLogin()}
              fallback={
                <>
                  {t("auth.switch.has_account")}{" "}
                  <button
                    type="button"
                    class="font-medium text-text-display underline-offset-2 hover:underline"
                    onClick={() => setMode("login")}
                  >
                    {t("auth.switch.signin_here")}
                  </button>
                </>
              }
            >
              <>
                {t("auth.switch.no_account")}{" "}
                <button
                  type="button"
                  class="font-medium text-text-display underline-offset-2 hover:underline"
                  onClick={() => setMode("register")}
                >
                  {t("auth.switch.create_one")}
                </button>
              </>
            </Show>
          </div>
        </form>

        {/* 3-pillar tagline */}
        <ul
          class="mt-8 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[0.7rem] text-text-mute"
          aria-label="AMOR principles"
        >
          <li class="flex items-center gap-1.5">
            <span
              class="inline-block h-1.5 w-1.5 rounded-full"
              style={{ background: "var(--color-mode-research)" }}
              aria-hidden="true"
            />
            {t("auth.tag.privacy")}
          </li>
          <li class="flex items-center gap-1.5">
            <span
              class="inline-block h-1.5 w-1.5 rounded-full"
              style={{ background: "var(--color-mode-build)" }}
              aria-hidden="true"
            />
            {t("auth.tag.offline")}
          </li>
          <li class="flex items-center gap-1.5">
            <span
              class="inline-block h-1.5 w-1.5 rounded-full"
              style={{ background: "var(--color-mode-thinking)" }}
              aria-hidden="true"
            />
            {t("auth.tag.opensource")}
          </li>
        </ul>
      </div>
    </div>
  );
};
