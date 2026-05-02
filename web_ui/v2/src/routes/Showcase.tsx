import { type Component, For } from "solid-js";

/**
 * Component showcase — Storybook-lite preview surface.  Used to
 * verify atom rendering + theme tokens + per-mode accent shifts
 * without spinning up the real chat shell.  Lives at /showcase.
 *
 * In PR-2 this route gains real atoms (Button, Input, Badge,
 * StatusPill, Spinner, ProgressBar, Kbd, Avatar, Divider, Tooltip).
 * For now it renders the design-token palette + the 6 mode accents
 * so we can eyeball the OKLCH values in both light and dark.
 */
const MODES: ReadonlyArray<{
  key: string;
  label: string;
  glyph: string;
  subtitle: string;
}> = [
  { key: "research", label: "Research", glyph: "compass", subtitle: "gather, summarise, cite" },
  { key: "thinking", label: "Thinking", glyph: "brain", subtitle: "multi-step reasoning" },
  { key: "build", label: "Build", glyph: "hammer", subtitle: "code, test, debug" },
  { key: "consortium", label: "Consortium", glyph: "users-round", subtitle: "research + think + build" },
  { key: "sentinel", label: "Sentinel", glyph: "shield-half", subtitle: "governance, ledger" },
  { key: "system", label: "System", glyph: "activity", subtitle: "diagnostics, memory" },
];

const STATUS_PILLS: ReadonlyArray<{ key: string; label: string; varName: string }> = [
  { key: "healthy", label: "Healthy", varName: "--color-status-healthy" },
  { key: "warming", label: "Warming", varName: "--color-status-warming" },
  { key: "warning", label: "Warning", varName: "--color-status-warning" },
  { key: "failed", label: "Failed", varName: "--color-status-failed" },
];

export const Showcase: Component = () => {
  const toggleTheme = () => {
    const html = document.documentElement;
    const cur = html.getAttribute("data-theme") ?? "system";
    const next = cur === "dark" ? "light" : cur === "light" ? "system" : "dark";
    html.setAttribute("data-theme", next);
  };

  return (
    <main
      data-mode="system"
      class="min-h-screen bg-bg-primary p-8 text-text-primary"
    >
      <header class="mx-auto mb-8 flex max-w-5xl items-center justify-between border-b border-border-subtle pb-4">
        <div>
          <h1 class="text-2xl font-semibold tracking-tight">Component Showcase</h1>
          <p class="mt-1 text-sm text-text-secondary">
            PR-1 scaffold &middot; Tailwind v4 @theme tokens
          </p>
        </div>
        <div class="flex items-center gap-2 text-sm">
          <a href="/" class="text-text-secondary hover:text-text-primary">
            &larr; Back
          </a>
          <button
            type="button"
            onClick={toggleTheme}
            class="rounded-md border border-border-default bg-bg-elevated px-3 py-1.5 hover:bg-bg-hover"
          >
            Toggle theme
          </button>
        </div>
      </header>

      <div class="mx-auto max-w-5xl space-y-10">
        {/* Mode accents */}
        <section>
          <h2 class="mb-3 text-lg font-medium">Mode accents</h2>
          <p class="mb-4 text-sm text-text-secondary">
            One CSS variable shifts per mode.  Chrome stays monochrome;
            only the focus ring, timeline pill, and header rule pull
            from <code>--mode-accent</code>.
          </p>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3">
            <For each={MODES}>
              {(mode) => (
                <div
                  data-mode={mode.key}
                  class="rounded-lg border border-border-subtle bg-bg-elevated p-4"
                >
                  <div class="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      class="h-3 w-3 rounded-full"
                      style={{ background: "var(--mode-accent)" }}
                    />
                    <span class="font-medium">{mode.label}</span>
                  </div>
                  <p class="mt-1 text-xs text-text-tertiary">{mode.subtitle}</p>
                  <p class="mt-2 text-xs font-mono text-text-secondary">
                    icon: {mode.glyph}
                  </p>
                </div>
              )}
            </For>
          </div>
        </section>

        {/* Status pills */}
        <section>
          <h2 class="mb-3 text-lg font-medium">Status pills</h2>
          <div class="flex flex-wrap gap-3">
            <For each={STATUS_PILLS}>
              {(pill) => (
                <div class="flex items-center gap-2 rounded-full border border-border-subtle bg-bg-elevated px-3 py-1 text-sm">
                  <span
                    aria-hidden="true"
                    class="h-2 w-2 rounded-full"
                    style={{ background: `var(${pill.varName})` }}
                  />
                  <span>{pill.label}</span>
                </div>
              )}
            </For>
          </div>
        </section>

        {/* Spacing scale */}
        <section>
          <h2 class="mb-3 text-lg font-medium">Spacing scale (8-pt)</h2>
          <div class="space-y-2">
            <For each={[1, 2, 3, 4, 5, 6, 7, 8, 9]}>
              {(n) => (
                <div class="flex items-center gap-3 text-sm">
                  <span class="w-12 font-mono text-xs text-text-tertiary">
                    space-{n}
                  </span>
                  <span
                    class="block bg-text-primary"
                    style={{
                      height: `var(--spacing-${n})`,
                      width: `var(--spacing-${n})`,
                    }}
                  />
                </div>
              )}
            </For>
          </div>
        </section>

        {/* Type scale */}
        <section>
          <h2 class="mb-3 text-lg font-medium">Typography</h2>
          <div class="space-y-2">
            <p class="text-xs">xs &middot; The quick brown fox</p>
            <p class="text-sm">sm &middot; The quick brown fox</p>
            <p class="text-base">base &middot; The quick brown fox</p>
            <p class="text-lg">lg &middot; The quick brown fox</p>
            <p class="text-xl">xl &middot; The quick brown fox</p>
            <p class="text-2xl font-semibold">2xl &middot; The quick brown fox</p>
            <p class="text-3xl font-semibold">3xl &middot; The quick brown fox</p>
            <p class="text-4xl font-bold">4xl &middot; The quick brown fox</p>
          </div>
        </section>
      </div>
    </main>
  );
};
