import { type Component, For, Show } from "solid-js";
import { A, useLocation } from "@solidjs/router";
import { MODES, type ModeMeta } from "../../lib/types";
import { auth } from "../../lib/auth";
import { Avatar, Tooltip, IconButton, Kbd } from "../ui";
import { SessionList } from "./SessionList";
import { modeLabel, t } from "../../i18n";

interface SystemLink {
  href: string;
  /** i18n key ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â looked up via ``t()`` at render time. */
  label_key: string;
  glyph: string;
  external?: boolean;
}

const SYSTEM_LINKS: ReadonlyArray<SystemLink> = [
  { href: "/settings", label_key: "sidebar.system.settings", glyph: "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢" },
  // Cycle C Sprint 0 Day 3 ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â baseline corpus dashboard.  Lives under
  // System on purpose: it's an operator/owner view, not a per-mode
  // workspace.  When Sprint 6's training tab lands it'll join here.
  { href: "/admin/baselines", label_key: "sidebar.system.baselines", glyph: "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¦" },
  // Cycle C Sprint 1 Day 4 ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â LLM backend dashboard (resident models,
  // swap events, p50/p95 latency).
  { href: "/admin/llm", label_key: "sidebar.system.llm", glyph: "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â " },
  // Cycle C Sprint 2 Day 5 ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â eval harness dashboard.
  { href: "/admin/evals", label_key: "sidebar.system.evals", glyph: "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸" },
];

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
  onOpenPalette: () => void;
}

/** Lucide-style glyph fallback ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â we render an emoji-class character
 *  per mode until we drop a real Lucide-Solid binding in.  Same
 *  visual weight, zero deps. */
const GLYPH: Record<string, string> = {
  compass: "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â½",
  hammer: "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â²",
  brain: "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â ",
  "users-round": "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œ",
  "shield-half": "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â",
  activity: "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â°ÃƒÆ’Ã¢â‚¬Â¹ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ",
};

export const Sidebar: Component<SidebarProps> = (props) => {
  const location = useLocation();
  const isActive = (href: string) =>
    location.pathname === href ||
    (href !== "/" && location.pathname.startsWith(href));

  return (
    <aside
      class={[
        "flex h-full flex-col border-r border-border-subtle bg-bg-elevated-v25",
        "transition-[width] duration-200",
        props.collapsed ? "w-14" : "w-60",
      ].join(" ")}
      aria-label="Primary"
    >
      {/* Brand + collapse */}
      <div class="flex h-12 items-center justify-between border-b border-border-subtle px-3">
        <Show when={!props.collapsed}>
          <A href="/" class="font-semibold tracking-tight">
            {t("sidebar.brand")}
          </A>
        </Show>
        <IconButton
          aria-label={props.collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
          size="sm"
          onClick={props.onToggle}
        >
          <span aria-hidden="true">{props.collapsed ? "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âº" : "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¹"}</span>
        </IconButton>
      </div>

      {/* Command palette trigger */}
      <Show when={!props.collapsed}>
        <button
          type="button"
          onClick={props.onOpenPalette}
          class="mx-3 mt-3 flex items-center justify-between rounded-md border border-border-subtle bg-bg-elevated px-3 py-1.5 text-xs text-text-subtle hover:bg-bg-hover hover:text-text-body"
          aria-label={t("sidebar.palette_open")}
        >
          <span>{t("sidebar.search")}</span>
          <Kbd>Mod+K</Kbd>
        </button>
      </Show>
      <Show when={props.collapsed}>
        <div class="mx-2 mt-3 flex justify-center">
          <Tooltip label={`${t("palette.dialog_label")} (ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã¢â‚¬Â¹Ãƒâ€¦Ã¢â‚¬Å“K)`} placement="right">
            <IconButton
              aria-label={t("sidebar.palette_open")}
              size="sm"
              onClick={props.onOpenPalette}
            >
              <span aria-hidden="true">ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã¢â‚¬Â¹Ãƒâ€¦Ã¢â‚¬Å“</span>
            </IconButton>
          </Tooltip>
        </div>
      </Show>

      {/* Modes */}
      <nav class="flex-1 overflow-y-auto px-2 py-3">
        <Show when={!props.collapsed}>
          <p class="mb-1.5 px-2 text-[0.65rem] font-semibold uppercase tracking-widest text-text-subtle">
            {t("sidebar.section.modes")}
          </p>
        </Show>
        <ul class="space-y-0.5">
          <For each={MODES}>
            {(mode) => (
              <SidebarItem mode={mode} active={isActive(mode.href)} collapsed={props.collapsed} />
            )}
          </For>
        </ul>

        <Show when={!props.collapsed}>
          <p class="mt-6 mb-1.5 px-2 text-[0.65rem] font-semibold uppercase tracking-widest text-text-subtle">
            {t("sidebar.section.system")}
          </p>
        </Show>
        <ul class="space-y-0.5">
          <For each={SYSTEM_LINKS}>
            {(link) => (
              <li>
                <Show
                  when={link.external}
                  fallback={
                    <A
                      href={link.href}
                      class={[
                        "flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm",
                        "text-text-body hover:bg-bg-hover hover:text-text-display",
                        isActive(link.href)
                          ? "bg-bg-hover text-text-display"
                          : "",
                      ].join(" ")}
                      end={link.href === "/"}
                    >
                      <span
                        class="w-4 text-center text-text-subtle"
                        aria-hidden="true"
                      >
                        {link.glyph}
                      </span>
                      <Show when={!props.collapsed}>
                        <span class="truncate">{t(link.label_key)}</span>
                      </Show>
                    </A>
                  }
                >
                  <a
                    href={link.href}
                    class={[
                      "flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm",
                      "text-text-body hover:bg-bg-hover hover:text-text-display",
                    ].join(" ")}
                  >
                    <span
                      class="w-4 text-center text-text-subtle"
                      aria-hidden="true"
                    >
                      {link.glyph}
                    </span>
                    <Show when={!props.collapsed}>
                      <span class="truncate">{t(link.label_key)}</span>
                    </Show>
                  </a>
                </Show>
              </li>
            )}
          </For>
        </ul>

        {/* Sessions */}
        <SessionList collapsed={props.collapsed} />
      </nav>

      {/* User card */}
      <div class="border-t border-border-subtle px-3 py-3">
        <Show
          when={auth.user()}
          fallback={
            <Show when={!props.collapsed}>
              <p class="text-xs text-text-subtle">{t("sidebar.signed_out")}</p>
            </Show>
          }
        >
          {(u) => (
            <div class="flex items-center gap-2">
              <Avatar
                variant="user"
                initials={(u().display_name ?? u().username).slice(0, 2)}
                size={24}
              />
              <Show when={!props.collapsed}>
                <div class="min-w-0 flex-1">
                  <p class="truncate text-xs font-medium">
                    {u().display_name ?? u().username}
                  </p>
                  <p class="truncate text-[0.65rem] text-text-subtle">
                    {u().email}
                  </p>
                </div>
              </Show>
            </div>
          )}
        </Show>
      </div>
    </aside>
  );
};

const SidebarItem: Component<{
  mode: ModeMeta;
  active: boolean;
  collapsed: boolean;
}> = (props) => {
  const link = (
    <A
      href={props.mode.href}
      class={[
        "group flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm",
        "text-text-body hover:bg-bg-hover hover:text-text-display",
        props.active ? "bg-bg-hover text-text-display" : "",
      ].join(" ")}
      data-mode={props.mode.key}
    >
      <span
        class={[
          "flex h-4 w-4 items-center justify-center text-[0.85rem]",
          props.active ? "text-text-display" : "text-text-subtle",
        ].join(" ")}
        aria-hidden="true"
        style={
          props.active ? { color: "var(--mode-accent)" } : undefined
        }
      >
        {GLYPH[props.mode.glyph] ?? "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢"}
      </span>
      <Show when={!props.collapsed}>
        <span class="flex-1 truncate">{modeLabel(props.mode)}</span>
        <Show when={!props.mode.wired}>
          <span class="rounded-full bg-bg-elevated-v25 px-1.5 py-0.5 text-[0.55rem] uppercase tracking-wide text-text-subtle">
            {t("sidebar.mode.soon")}
          </span>
        </Show>
      </Show>
    </A>
  );

  return (
    <li>
      <Show
        when={!props.collapsed}
        fallback={
          <Tooltip label={modeLabel(props.mode)} placement="right">
            {link}
          </Tooltip>
        }
      >
        {link}
      </Show>
    </li>
  );
};
