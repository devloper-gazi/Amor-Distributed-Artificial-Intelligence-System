import { type Component, For } from "solid-js";
import { A } from "@solidjs/router";
import { MODES } from "../lib/types";
import { TopBar } from "../components/shell/TopBar";
import { Badge } from "../components/ui";
import { t, modeLabel, modeSubtitle } from "../i18n";

const GLYPH: Record<string, string> = {
  compass: "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬ÂÃƒâ€¦Ã‚Â½",
  hammer: "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“Ãƒâ€šÃ‚Â²",
  brain: "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬ÂÃƒâ€¦Ã‚Â ",
  "users-round": "ÃƒÆ’Ã‚Â¢Ãƒâ€šÃ‚ÂÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“",
  "shield-half": "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬ÂÃƒâ€šÃ‚Â",
  activity: "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â°Ãƒâ€¹Ã¢â‚¬Â ",
};

/**
 * Welcome / mode picker.  Renders the 6 modes as cards; each card
 * gets the mode accent colour as a top rule.  Cycle D ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â fully
 * localised via the i18n primitive (no more hardcoded English).
 */
export const Home: Component = () => {
  return (
    <div data-mode="system" class="flex h-full flex-col">
      <TopBar
        title={t("home.title")}
        subtitle={t("home.subtitle")}
      />
      <div class="flex-1 overflow-y-auto px-6 py-8">
        <div class="mx-auto grid max-w-4xl grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <For each={MODES}>
            {(mode) => (
              <A
                href={mode.href}
                data-mode={mode.key}
                class={[
                  "group relative flex flex-col gap-2 rounded-lg",
                  "border border-border-subtle bg-bg-elevated p-5",
                  "transition-colors duration-150 hover:bg-bg-hover",
                ].join(" ")}
              >
                <span
                  class="absolute inset-x-0 top-0 h-0.5 rounded-t-lg"
                  style={{ background: "var(--mode-accent)" }}
                  aria-hidden="true"
                />
                <div class="flex items-start justify-between">
                  <span
                    class="text-lg"
                    aria-hidden="true"
                    style={{ color: "var(--mode-accent)" }}
                  >
                    {GLYPH[mode.glyph] ?? "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¢"}
                  </span>
                  {mode.wired ? null : (
                    <Badge size="sm">{t("home.soon")}</Badge>
                  )}
                </div>
                <h2 class="text-base font-semibold tracking-tight">
                  {modeLabel(mode)}
                </h2>
                <p class="text-xs text-text-body">{modeSubtitle(mode)}</p>
                <span class="mt-2 text-xs text-text-subtle group-hover:text-text-body">
                  {t("home.open")}
                </span>
              </A>
            )}
          </For>
        </div>

        <div class="mx-auto mt-10 max-w-4xl rounded-lg border border-border-subtle bg-bg-elevated-v25 p-5 text-sm">
          <p class="font-medium">{t("home.tip.title")}</p>
          <p class="mt-1 text-text-body">
            {t("home.tip.body", {
              cmdK: "ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬â„¢Ãƒâ€¹Ã…â€œK",
              ctrlK: "Ctrl K",
            })
              .split(/(ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬â„¢Ãƒâ€¹Ã…â€œK|Ctrl K)/)
              .map((part) =>
                part === "ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬â„¢Ãƒâ€¹Ã…â€œK" || part === "Ctrl K" ? (
                  <kbd class="rounded border border-border-subtle bg-bg-elevated-v25 px-1.5 py-0.5 font-mono text-xs">
                    {part}
                  </kbd>
                ) : (
                  part
                ),
              )}
          </p>
        </div>
      </div>
    </div>
  );
};
