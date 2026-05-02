import { type Component, createSignal, onMount, For } from "solid-js";
import { useNavigate } from "@solidjs/router";
import { TopBar } from "../components/shell/TopBar";
import { Button } from "../components/ui";
import { auth } from "../lib/auth";

type Theme = "light" | "dark" | "system";
const THEMES: ReadonlyArray<{ key: Theme; label: string; subtitle: string }> = [
  { key: "light", label: "Light", subtitle: "always light, ignores OS" },
  { key: "dark", label: "Dark", subtitle: "always dark, ignores OS" },
  { key: "system", label: "System", subtitle: "follow OS preference" },
];

export const Settings: Component = () => {
  const nav = useNavigate();
  const [theme, setTheme] = createSignal<Theme>("system");

  onMount(() => {
    try {
      const saved = (localStorage.getItem("amor.theme") ?? "system") as Theme;
      setTheme(saved);
    } catch {
      // ignore
    }
  });

  const applyTheme = (t: Theme) => {
    document.documentElement.setAttribute("data-theme", t);
    try {
      localStorage.setItem("amor.theme", t);
    } catch {
      // ignore
    }
    setTheme(t);
  };

  const onLogout = async () => {
    await auth.logout();
    nav("/login", { replace: true });
  };

  return (
    <div data-mode="system" class="flex h-full flex-col">
      <TopBar title="Settings" subtitle="theme, account" />
      <div class="flex-1 overflow-y-auto px-6 py-8">
        <div class="mx-auto max-w-2xl space-y-8">
          {/* Theme */}
          <section>
            <h2 class="text-base font-semibold tracking-tight">Theme</h2>
            <p class="mt-1 text-sm text-text-secondary">
              Persists in <code>localStorage</code>.  System mode follows
              your OS preference automatically.
            </p>
            <div class="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
              <For each={THEMES}>
                {(t) => (
                  <button
                    type="button"
                    onClick={() => applyTheme(t.key)}
                    class={[
                      "rounded-md border px-4 py-3 text-left transition-colors",
                      theme() === t.key
                        ? "border-text-primary bg-bg-hover"
                        : "border-border-subtle bg-bg-elevated hover:bg-bg-hover",
                    ].join(" ")}
                    aria-pressed={theme() === t.key}
                  >
                    <div class="text-sm font-medium">{t.label}</div>
                    <div class="text-xs text-text-tertiary">{t.subtitle}</div>
                  </button>
                )}
              </For>
            </div>
          </section>

          {/* Account */}
          <section>
            <h2 class="text-base font-semibold tracking-tight">Account</h2>
            <div class="mt-3 rounded-md border border-border-subtle bg-bg-elevated p-4 text-sm">
              <p class="text-text-secondary">
                Signed in as{" "}
                <span class="font-medium text-text-primary">
                  {auth.user()?.username ?? "—"}
                </span>
              </p>
              <p class="mt-1 text-xs text-text-tertiary">
                {auth.user()?.email ?? ""}
              </p>
              <div class="mt-4 flex gap-2">
                <Button variant="secondary" size="sm" onClick={onLogout}>
                  Sign out
                </Button>
              </div>
            </div>
          </section>

          {/* Component showcase */}
          <section>
            <h2 class="text-base font-semibold tracking-tight">Developer</h2>
            <div class="mt-3 rounded-md border border-border-subtle bg-bg-elevated p-4 text-sm">
              <p class="text-text-secondary">
                Inspect every atom + theme token in one place.
              </p>
              <a
                href="/showcase"
                class="mt-3 inline-flex h-8 items-center rounded-md border border-border-default bg-bg-elevated px-3 text-sm hover:bg-bg-hover"
              >
                Open component showcase
              </a>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
};
