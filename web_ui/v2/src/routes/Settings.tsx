import { type Component, createSignal, onMount, For } from "solid-js";
import { useNavigate } from "@solidjs/router";
import { TopBar } from "../components/shell/TopBar";
import { Button } from "../components/ui";
import { auth } from "../lib/auth";
import {
  type Locale,
  getSupportedLocales,
  locale,
  setLocale,
  t,
} from "../i18n";

type Theme = "light" | "dark" | "system";
const THEMES: ReadonlyArray<{ key: Theme; label_key: string; subtitle_key: string }> = [
  { key: "light",  label_key: "settings.theme.light",  subtitle_key: "settings.theme.light_subtitle"  },
  { key: "dark",   label_key: "settings.theme.dark",   subtitle_key: "settings.theme.dark_subtitle"   },
  { key: "system", label_key: "settings.theme.system", subtitle_key: "settings.theme.system_subtitle" },
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
      <TopBar
        title={t("settings.title")}
        subtitle={t("settings.subtitle")}
      />
      <div class="flex-1 overflow-y-auto px-6 py-8">
        <div class="mx-auto max-w-2xl space-y-8">
          {/* Theme */}
          <section>
            <h2 class="text-base font-semibold tracking-tight">
              {t("settings.theme.heading")}
            </h2>
            <p class="mt-1 text-sm text-text-body">
              {t("settings.theme.description")}
            </p>
            <div class="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
              <For each={THEMES}>
                {(opt) => (
                  <button
                    type="button"
                    onClick={() => applyTheme(opt.key)}
                    class={[
                      "rounded-md border px-4 py-3 text-left transition-colors",
                      theme() === opt.key
                        ? "border-text-primary bg-bg-hover"
                        : "border-border-subtle bg-bg-elevated hover:bg-bg-hover",
                    ].join(" ")}
                    aria-pressed={theme() === opt.key}
                  >
                    <div class="text-sm font-medium">
                      {t(opt.label_key)}
                    </div>
                    <div class="text-xs text-text-subtle">
                      {t(opt.subtitle_key)}
                    </div>
                  </button>
                )}
              </For>
            </div>
          </section>

          {/* Language — Sprint 10 Day 1 */}
          <section>
            <h2 class="text-base font-semibold tracking-tight">
              {t("settings.language.heading")}
            </h2>
            <p class="mt-1 text-sm text-text-body">
              {t("settings.language.description")}
            </p>
            <div class="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
              <For each={getSupportedLocales()}>
                {(loc: Locale) => (
                  <button
                    type="button"
                    onClick={() => setLocale(loc)}
                    class={[
                      "rounded-md border px-4 py-3 text-left transition-colors",
                      locale() === loc
                        ? "border-text-primary bg-bg-hover"
                        : "border-border-subtle bg-bg-elevated hover:bg-bg-hover",
                    ].join(" ")}
                    aria-pressed={locale() === loc}
                    data-amor-locale={loc}
                  >
                    <div class="text-sm font-medium">
                      {t(`settings.language.${loc}`)}
                    </div>
                    <div class="text-xs text-text-subtle">
                      {t(`settings.language.${loc}_subtitle`)}
                    </div>
                  </button>
                )}
              </For>
            </div>
          </section>

          {/* Account */}
          <section>
            <h2 class="text-base font-semibold tracking-tight">
              {t("settings.account.heading")}
            </h2>
            <div class="mt-3 rounded-md border border-border-subtle bg-bg-elevated p-4 text-sm">
              <p class="text-text-body">
                {t("settings.account.signed_in_as")}{" "}
                <span class="font-medium text-text-display">
                  {auth.user()?.username ?? "—"}
                </span>
              </p>
              <p class="mt-1 text-xs text-text-subtle">
                {auth.user()?.email ?? ""}
              </p>
              <div class="mt-4 flex gap-2">
                <Button variant="secondary" size="sm" onClick={onLogout}>
                  {t("settings.account.sign_out")}
                </Button>
              </div>
            </div>
          </section>

          {/* Component showcase */}
          <section>
            <h2 class="text-base font-semibold tracking-tight">
              {t("settings.developer.heading")}
            </h2>
            <div class="mt-3 rounded-md border border-border-subtle bg-bg-elevated p-4 text-sm">
              <p class="text-text-body">
                {t("settings.developer.description")}
              </p>
              <a
                href="/showcase"
                class="mt-3 inline-flex h-8 items-center rounded-md border border-border-strong-v25 bg-bg-elevated px-3 text-sm hover:bg-bg-hover"
              >
                {t("settings.developer.cta")}
              </a>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
};
